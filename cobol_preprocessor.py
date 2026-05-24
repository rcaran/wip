"""
cobol_preprocessor.py
=====================
Pré-processador COBOL → JSON estruturado para extração de regras de negócio via LLM.

Correções e melhorias sobre a versão anterior:
  1. CFG corrigido: IF aninhados usam pilha para _pending_merge
  2. visitIfStatement não chama visitChildren após travessia manual
  3. Lista de palavras reservadas COBOL para evitar falsos positivos
  4. Serialização recursiva de Variable sem quebrar JSON
  5. Rastreamento de leituras/escritas por variável por parágrafo
  6. Detecção de padrões semânticos (acumulador, flag-88, controle de arquivo)
  7. Output sumarizado por parágrafo/seção — menos tokens, mais contexto
  8. Detecção de dead code (nós sem predecessores)
  9. Prompt otimizado para Opus 4 extrair 100% das regras de negócio
"""

import json
import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple

# ----------------------------------------------------------------------
# Imports da gramática ANTLR gerada (substituir pelos caminhos reais)
# from Cobol85Lexer import Cobol85Lexer
# from Cobol85Parser import Cobol85Parser
# from Cobol85Listener import Cobol85Listener
# from Cobol85Visitor import Cobol85Visitor
# from antlr4 import InputStream, CommonTokenStream, ParseTreeWalker
# from antlr4.error.ErrorListener import ErrorListener
# ----------------------------------------------------------------------


# ======================================================================
# 0. Palavras reservadas COBOL — excluídas da resolução de símbolos
# ======================================================================
COBOL_RESERVED = {
    "ACCEPT", "ADD", "ALTER", "CALL", "CANCEL", "CLOSE", "COMPUTE",
    "CONTINUE", "DELETE", "DISPLAY", "DIVIDE", "ELSE", "END", "END-ADD",
    "END-CALL", "END-COMPUTE", "END-DELETE", "END-DIVIDE", "END-EVALUATE",
    "END-IF", "END-MULTIPLY", "END-PERFORM", "END-READ", "END-REWRITE",
    "END-SEARCH", "END-START", "END-STRING", "END-SUBTRACT", "END-UNSTRING",
    "END-WRITE", "EVALUATE", "EXIT", "FUNCTION", "GO", "GOBACK", "IF",
    "INITIALIZE", "INSPECT", "MERGE", "MOVE", "MULTIPLY", "NEXT",
    "NOT", "OPEN", "OTHER", "PERFORM", "READ", "REWRITE", "SEARCH",
    "SET", "SORT", "START", "STOP", "STRING", "SUBTRACT", "THRU",
    "UNSTRING", "UNTIL", "VARYING", "WHEN", "WRITE", "THEN", "TO",
    "FROM", "INTO", "BY", "OF", "IN", "AT", "WITH", "AFTER", "BEFORE",
    "GIVING", "RETURNING", "USING", "DEPENDING", "ON", "AND", "OR",
    "TRUE", "FALSE", "ZEROS", "ZEROES", "SPACES", "LOW-VALUE", "HIGH-VALUE",
    "QUOTE", "ALL", "LENGTH", "SECTION", "PROCEDURE", "DIVISION", "DATA",
    "WORKING-STORAGE", "LINKAGE", "FILE", "ENVIRONMENT", "IDENTIFICATION",
    "PROGRAM-ID", "AUTHOR", "DATE", "CONFIGURATION", "PIC", "PICTURE",
    "COMP", "COMP-3", "COMP-5", "BINARY", "PACKED-DECIMAL", "DISPLAY",
    "OCCURS", "TIMES", "REDEFINES", "RENAMES", "VALUE", "VALUES",
    "LEADING", "TRAILING", "SEPARATE", "SIGN", "SYNCHRONIZED", "JUST",
    "JUSTIFIED", "RIGHT", "LEFT", "BLANK", "ZERO", "SPACE", "STANDARD",
    "OPTIONAL", "REQUIRED", "FULL", "AUTO", "SECURE", "ERASE", "EOL",
    "EOS", "HIGHLIGHT", "LOWLIGHT", "REVERSE-VIDEO", "BLINK", "UNDERLINE",
    "COLUMN", "COL", "LINE", "LINES", "COLUMNS", "COLS", "SCROLL",
    "EXEC", "SQL", "CICS", "END-EXEC",
}


# ======================================================================
# 1. Estruturas de dados
# ======================================================================

@dataclass
class Variable:
    name: str
    level: int
    pic: Optional[str] = None
    value: Optional[str] = None
    occurs: Optional[int] = None
    redefines: Optional[str] = None
    usage: Optional[str] = None
    is_88: bool = False
    condition_values: List[str] = field(default_factory=list)
    children: List["Variable"] = field(default_factory=list)

    def to_dict(self) -> dict:
        """Serialização recursiva segura para JSON."""
        return {
            "name": self.name,
            "level": self.level,
            "pic": self.pic,
            "value": self.value,
            "occurs": self.occurs,
            "redefines": self.redefines,
            "usage": self.usage,
            "is_88": self.is_88,
            "condition_values": self.condition_values,
            "children": [c.to_dict() for c in self.children],
        }


@dataclass
class FileDescriptor:
    name: str
    organization: Optional[str] = None
    access: Optional[str] = None
    file_status: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "organization": self.organization,
            "access": self.access,
            "file_status": self.file_status,
        }


@dataclass
class DataDivision:
    working_storage: List[Variable] = field(default_factory=list)
    linkage: List[Variable] = field(default_factory=list)
    files: List[FileDescriptor] = field(default_factory=list)


@dataclass
class GNode:
    id: str
    kind: str
    statement: str
    line: int
    scope: str = ""
    meta: dict = field(default_factory=dict)


@dataclass
class GEdge:
    source: str
    target: str
    relation: str


# ======================================================================
# 2. Listener para Data Division
# ======================================================================

class DataDivisionListener:  # herda de Cobol85Listener em produção
    def __init__(self):
        self.data = DataDivision()
        self._current_section: Optional[str] = None
        self._stack: List[Variable] = []

    # --- Seções ---
    def enterWorkingStorageSection(self, ctx):
        self._current_section = "WORKING-STORAGE"
        self._stack = []

    def enterLinkageSection(self, ctx):
        self._current_section = "LINKAGE"
        self._stack = []

    def enterFileSection(self, ctx):
        self._current_section = "FILE"
        self._stack = []

    def exitDataDivision(self, ctx):
        self._current_section = None

    # --- Arquivos ---
    def enterFileDescriptionEntry(self, ctx):
        name = ctx.fileName().getText() if hasattr(ctx, "fileName") else "UNKNOWN"
        fd = FileDescriptor(name=name)

        # Tentar capturar ORGANIZATION e ACCESS MODE
        text = ctx.getText().upper()
        for org in ("SEQUENTIAL", "INDEXED", "RELATIVE"):
            if org in text:
                fd.organization = org
                break
        for acc in ("SEQUENTIAL", "RANDOM", "DYNAMIC"):
            if acc in text:
                fd.access = acc
                break

        # FILE STATUS
        m = re.search(r"FILE\s*STATUS\s+(?:IS\s+)?(\S+)", text, re.IGNORECASE)
        if m:
            fd.file_status = m.group(1).rstrip(".")

        self.data.files.append(fd)

    # --- Variáveis ---
    def enterDataDescriptionEntry(self, ctx):
        # Extrai nível e nome de forma defensiva
        try:
            level_token = ctx.INTEGERLITERAL()
            level = int(level_token.getText()) if level_token else 0
        except Exception:
            level = 0

        name = ""
        try:
            name = ctx.entryName().getText() if hasattr(ctx, "entryName") and ctx.entryName() else ""
        except Exception:
            pass

        # PIC
        pic = None
        try:
            if hasattr(ctx, "PICClause") and ctx.PICClause():
                pic = ctx.PICClause().getText()
        except Exception:
            pass

        is_88 = level == 88
        value = None
        cond_vals: List[str] = []

        # VALUE
        try:
            if hasattr(ctx, "valueClause") and ctx.valueClause():
                val_text = ctx.valueClause().getText()
                # Remove a keyword VALUE
                val_clean = re.sub(r"(?i)^VALUE(S)?\s+(IS\s+|ARE\s+)?", "", val_text).strip()
                value = val_clean
                if is_88:
                    pairs = re.findall(r"'([^']*)'|\"([^\"]*)\"", val_text)
                    cond_vals = [m[0] or m[1] for m in pairs]
                    # Suporte a intervalos numéricos
                    nums = re.findall(r"\b(\d+(?:\.\d+)?)\s+THRU\s+(\d+(?:\.\d+)?)\b", val_text, re.I)
                    for lo, hi in nums:
                        cond_vals.append(f"{lo} THRU {hi}")
        except Exception:
            pass

        # OCCURS
        occurs = None
        try:
            if hasattr(ctx, "occursClause") and ctx.occursClause():
                occurs = int(ctx.occursClause().occursInteger().getText())
        except Exception:
            pass

        # REDEFINES
        redefines = None
        try:
            if hasattr(ctx, "redefinesClause") and ctx.redefinesClause():
                redefines = ctx.redefinesClause().dataName().getText()
        except Exception:
            pass

        # USAGE
        usage = None
        try:
            if hasattr(ctx, "usageClause") and ctx.usageClause():
                usage = ctx.usageClause().getText().upper()
        except Exception:
            pass

        var = Variable(
            level=level,
            name=name,
            pic=pic,
            value=value,
            occurs=occurs,
            redefines=redefines,
            usage=usage,
            is_88=is_88,
            condition_values=cond_vals,
        )

        target_list = (
            self.data.working_storage
            if self._current_section == "WORKING-STORAGE"
            else self.data.linkage
        )

        if level in (1, 77):
            target_list.append(var)
            self._stack = [var]
        elif level == 66:
            # RENAMES — trata como 01 simplificado
            target_list.append(var)
            self._stack = [var]
        else:
            # Subir na pilha até encontrar o pai correto
            while len(self._stack) > 1 and self._stack[-1].level >= level:
                self._stack.pop()
            if self._stack:
                self._stack[-1].children.append(var)
            self._stack.append(var)


# ======================================================================
# 3. Expansão de copybooks (com REPLACING e aninhamento)
# ======================================================================

def expand_copybooks(
    source: str, copy_lib: Dict[str, str], max_depth: int = 10
) -> Tuple[str, List[str]]:
    """
    Expande COPY statements substituindo pelo conteúdo do copybook.
    Retorna (source_expandido, lista_de_avisos).
    """
    warnings: List[str] = []
    # Suporta: COPY bookname [IN|OF library] [REPLACING ...].
    pattern = re.compile(
        r"(?i)COPY\s+(\S+?)(?:\s+(?:IN|OF)\s+\S+)?\s*"
        r"(REPLACING\s+(.*?)\s*)?\.",
        re.DOTALL,
    )

    def replacer(match: re.Match) -> str:
        book = match.group(1).strip().upper()
        replacing_clause = match.group(2)

        if book not in copy_lib and book.replace("-", "_") in copy_lib:
            book = book.replace("-", "_")

        if book not in copy_lib:
            warnings.append(f"COPY '{book}' não encontrado na biblioteca.")
            return match.group(0)  # mantém original

        content = copy_lib[book]

        if replacing_clause:
            # Extrai todos os pares: old BY new (suporta pseudo-texto ==...==)
            pairs = re.findall(
                r"(==.*?==|(?:'[^']*'|\"[^\"]*\"|\S+))\s+BY\s+"
                r"(==.*?==|(?:'[^']*'|\"[^\"]*\"|\S+))",
                replacing_clause,
                re.IGNORECASE,
            )
            for old, new in pairs:
                # Remove delimitadores == == ou aspas
                old_clean = re.sub(r"^==\s*|\s*==$|^['\"]|['\"]$", "", old.strip())
                new_clean = re.sub(r"^==\s*|\s*==$|^['\"]|['\"]$", "", new.strip())
                content = re.sub(
                    r"(?<![A-Za-z0-9\-])" + re.escape(old_clean) + r"(?![A-Za-z0-9\-])",
                    new_clean,
                    content,
                    flags=re.IGNORECASE,
                )
        return content

    previous = None
    for depth in range(max_depth):
        expanded = pattern.sub(replacer, source)
        if expanded == previous:
            break
        previous = source
        source = expanded
    else:
        warnings.append("Expansão de copybooks atingiu profundidade máxima — possível recursão.")

    return source, warnings


# ======================================================================
# 4. Rastreador de leitura/escrita por parágrafo
# ======================================================================

@dataclass
class ParagraphProfile:
    name: str
    section: str
    nodes: List[str] = field(default_factory=list)   # ids dos nós
    reads: Set[str] = field(default_factory=set)
    writes: Set[str] = field(default_factory=set)
    calls: List[str] = field(default_factory=list)
    performs: List[str] = field(default_factory=list)
    conditions: List[str] = field(default_factory=list)
    file_ops: List[dict] = field(default_factory=list)
    has_loop: bool = False
    has_exit: bool = False

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "section": self.section,
            "nodes": self.nodes,
            "reads": sorted(self.reads),
            "writes": sorted(self.writes),
            "calls": self.calls,
            "performs": self.performs,
            "conditions": self.conditions,
            "file_ops": self.file_ops,
            "has_loop": self.has_loop,
            "has_exit": self.has_exit,
        }


# ======================================================================
# 5. Visitor principal — CFG corrigido
# ======================================================================

class CobolRuleVisitor:  # herda de Cobol85Visitor em produção
    def __init__(self, data_division: DataDivision):
        self.nodes: List[GNode] = []
        self.edges: List[GEdge] = []
        self._last_id: Optional[str] = None
        self._section = "GLOBAL"
        self._paragraph = "GLOBAL"
        self._next_id = 0

        self.data = data_division
        self._var_map: Dict[str, Variable] = self._build_var_map(data_division)

        # Pilhas de controle de fluxo
        self._if_stack: List[dict] = []          # cada entrada: {cond_id}
        self._pending_merge_stack: List[dict] = []  # CORRIGIDO: pilha, não objeto único
        self._loop_stack: List[str] = []
        self._eval_stack: List[str] = []

        # Perfis por parágrafo
        self.paragraphs: Dict[str, ParagraphProfile] = {}
        self._current_para: Optional[ParagraphProfile] = None

    # ------------------------------------------------------------------
    # Utilitários
    # ------------------------------------------------------------------

    def _build_var_map(self, dd: DataDivision) -> Dict[str, Variable]:
        m: Dict[str, Variable] = {}

        def add(v: Variable):
            m[v.name.upper()] = v
            for c in v.children:
                add(c)

        for lst in (dd.working_storage, dd.linkage):
            for var in lst:
                add(var)
        return m

    def _gen_id(self) -> str:
        self._next_id += 1
        return str(self._next_id)

    def _current_scope(self) -> str:
        return f"{self._section}.{self._paragraph}".strip(".")

    def _add_node(
        self, kind: str, stmt: str, line: int, meta: dict
    ) -> GNode:
        n = GNode(
            id=self._gen_id(),
            kind=kind,
            statement=stmt,
            line=line,
            scope=self._current_scope(),
            meta=meta,
        )
        self.nodes.append(n)

        # Resolução de símbolos
        reads, writes = self._classify_variable_access(kind, stmt)
        n.meta["reads"] = sorted(reads)
        n.meta["writes"] = sorted(writes)

        # Atualizar perfil do parágrafo atual
        if self._current_para:
            self._current_para.nodes.append(n.id)
            self._current_para.reads.update(reads)
            self._current_para.writes.update(writes)
            if kind == "CALL":
                prog = meta.get("program", "")
                if prog:
                    self._current_para.calls.append(prog)
            if kind == "PERFORM":
                tgt = meta.get("target", "")
                if tgt:
                    self._current_para.performs.append(tgt)
            if kind == "IF":
                self._current_para.conditions.append(stmt)
            if kind in ("READ", "WRITE", "REWRITE", "DELETE", "OPEN", "CLOSE", "START"):
                self._current_para.file_ops.append({"op": kind, "stmt": stmt})
            if kind in ("EXIT", "GOBACK", "STOP"):
                self._current_para.has_exit = True

        # Processar junções pendentes (merge de IF/ELSE)
        while self._pending_merge_stack:
            pm = self._pending_merge_stack[-1]
            if pm.get("consumed"):
                self._pending_merge_stack.pop()
                continue
            # Conecta ramificações ao nó de merge
            for branch_last in (pm.get("then_last"), pm.get("else_last")):
                if branch_last:
                    self._add_edge(branch_last, n.id, "SEQUENCE")
            pm["consumed"] = True
            self._pending_merge_stack.pop()
            # NÃO adiciona aresta sequencial normal (já veio do merge)
            self._last_id = n.id
            return n

        # Ligação sequencial normal (exceto nós de estrutura)
        if self._last_id and kind not in ("IF", "EVALUATE", "ELSE"):
            self._add_edge(self._last_id, n.id, "SEQUENCE")

        self._last_id = n.id
        return n

    def _classify_variable_access(
        self, kind: str, stmt: str
    ) -> Tuple[Set[str], Set[str]]:
        """
        Determina quais variáveis são lidas e quais são escritas
        com base no tipo de comando e no texto.
        Exclui palavras reservadas COBOL.
        """
        words = set(re.findall(r"[A-Za-z][A-Za-z0-9\-]*", stmt))
        known_vars = {
            w.upper()
            for w in words
            if w.upper() in self._var_map and w.upper() not in COBOL_RESERVED
        }

        reads: Set[str] = set()
        writes: Set[str] = set()

        if kind == "MOVE":
            # MOVE X TO Y Z → X é lido, Y Z são escritos
            m = re.match(
                r"(?i)MOVE\s+(.+?)\s+TO\s+(.+?)(?:\s+END-MOVE)?$", stmt.strip()
            )
            if m:
                src_vars = {
                    w.upper()
                    for w in re.findall(r"[A-Za-z][A-Za-z0-9\-]*", m.group(1))
                    if w.upper() in self._var_map and w.upper() not in COBOL_RESERVED
                }
                dst_vars = {
                    w.upper()
                    for w in re.findall(r"[A-Za-z][A-Za-z0-9\-]*", m.group(2))
                    if w.upper() in self._var_map and w.upper() not in COBOL_RESERVED
                }
                reads = src_vars
                writes = dst_vars
            else:
                reads = known_vars

        elif kind in ("COMPUTE", "ADD", "SUBTRACT", "MULTIPLY", "DIVIDE"):
            # Heurística: tudo antes de GIVING é lido; GIVING e após é escrito
            m = re.search(r"(?i)GIVING\s+(.+?)(?:END-\w+)?$", stmt)
            if m:
                giving_vars = {
                    w.upper()
                    for w in re.findall(r"[A-Za-z][A-Za-z0-9\-]*", m.group(1))
                    if w.upper() in self._var_map and w.upper() not in COBOL_RESERVED
                }
                reads = known_vars - giving_vars
                writes = giving_vars
            else:
                # COMPUTE X = expr → X é escrito, resto é lido
                m2 = re.match(r"(?i)COMPUTE\s+(\S+)\s*=\s*(.+)", stmt.strip())
                if m2:
                    dest = m2.group(1).upper()
                    writes = {dest} if dest in self._var_map else set()
                    reads = known_vars - writes
                else:
                    # ADD X TO Y: Y é lido e escrito
                    reads = known_vars
                    writes = known_vars  # conservador

        elif kind in ("READ", "WRITE", "REWRITE", "DELETE"):
            # Variáveis de arquivo são lidas ou escritas
            reads = known_vars  # conservador
            writes = known_vars

        elif kind == "IF":
            reads = known_vars  # condição apenas lê
            writes = set()

        elif kind == "CALL":
            # Parâmetros USING podem ser lidos ou escritos
            reads = known_vars
            writes = set()

        elif kind in ("STRING", "UNSTRING"):
            reads = known_vars  # conservador
            writes = known_vars

        else:
            reads = known_vars
            writes = set()

        return reads, writes

    def _add_edge(self, source: str, target: str, relation: str):
        if source and target and source != target:
            self.edges.append(GEdge(source, target, relation))

    # ------------------------------------------------------------------
    # Escopos — Seção e Parágrafo
    # ------------------------------------------------------------------

    def visitSectionHeader(self, ctx):
        self._section = ctx.getText().replace(".", "").strip().upper()
        self._paragraph = ""
        return self.visitChildren(ctx)

    def visitParagraphHeader(self, ctx):
        para_name = ctx.getText().replace(".", "").strip().upper()
        self._paragraph = para_name
        key = self._current_scope()
        self.paragraphs[key] = ParagraphProfile(name=para_name, section=self._section)
        self._current_para = self.paragraphs[key]
        return self.visitChildren(ctx)

    # ------------------------------------------------------------------
    # IF — CORRIGIDO: pilha de pending_merge, sem visitChildren duplo
    # ------------------------------------------------------------------

    def visitIfStatement(self, ctx):
        # Texto da condição
        try:
            cond_text = ctx.condition().getText()
        except Exception:
            cond_text = ctx.getText().split("THEN")[0].replace("IF", "").strip()

        cond_node = self._add_node(
            "IF", cond_text, ctx.start.line, {"type": "CONDITION"}
        )

        # --- Ramo THEN ---
        saved_last = self._last_id
        # O ramo THEN começa após o nó IF via aresta TRUE
        self._last_id = cond_node.id

        if hasattr(ctx, "thenStatement") and ctx.thenStatement():
            self.visit(ctx.thenStatement())
        then_last = self._last_id

        # Aresta TRUE do IF → primeiro nó do THEN (já foi conectada pelo _add_node
        # pois _last_id era cond_node.id quando o primeiro nó do THEN foi criado).
        # Corrigimos o tipo da primeira aresta do THEN para TRUE.
        if self.edges and self.edges[-1].source == cond_node.id:
            self.edges[-1].relation = "TRUE"

        # --- Ramo ELSE ---
        else_last: Optional[str] = None
        if hasattr(ctx, "elseStatement") and ctx.elseStatement():
            else_marker = GNode(
                id=self._gen_id(),
                kind="ELSE",
                statement="ELSE",
                line=ctx.elseStatement().start.line,
                scope=self._current_scope(),
                meta={"type": "FALSE_BRANCH"},
            )
            self.nodes.append(else_marker)
            self._add_edge(cond_node.id, else_marker.id, "FALSE")

            self._last_id = else_marker.id
            self.visit(ctx.elseStatement())
            else_last = self._last_id

        # Registrar merge pendente na PILHA (não sobrescrever objeto único)
        self._pending_merge_stack.append(
            {
                "then_last": then_last,
                "else_last": else_last,
                "consumed": False,
            }
        )
        # O fluxo após o IF será resolvido quando o próximo nó for adicionado
        self._last_id = None

        # NÃO chama visitChildren — travessia já foi feita manualmente
        return None

    # ------------------------------------------------------------------
    # EVALUATE
    # ------------------------------------------------------------------

    def visitEvaluateStatement(self, ctx):
        try:
            subj = ctx.evaluateSubject().getText()
        except Exception:
            subj = ctx.getText().split("WHEN")[0].replace("EVALUATE", "").strip()

        eval_node = self._add_node(
            "EVALUATE", subj, ctx.start.line, {"type": "CONDITION"}
        )
        self._eval_stack.append(eval_node.id)

        whens = getattr(ctx, "whenClause", lambda: [])()
        branch_lasts: List[Optional[str]] = []

        for when in whens:
            when_text = when.getText()
            when_node = self._add_node(
                "WHEN", when_text, when.start.line, {"type": "CASE"}
            )
            self._add_edge(eval_node.id, when_node.id, "CASE")

            saved = self._last_id
            self._last_id = when_node.id
            self.visit(when)
            branch_lasts.append(self._last_id)
            self._last_id = saved  # cada WHEN parte do eval_node

        self._eval_stack.pop()

        # Todos os WHENs convergem no próximo nó
        if branch_lasts:
            # Registrar um pseudo-merge para todos os ramos
            for bl in branch_lasts:
                if bl:
                    self._pending_merge_stack.append(
                        {"then_last": bl, "else_last": None, "consumed": False}
                    )
        self._last_id = None
        return None

    # ------------------------------------------------------------------
    # PERFORM
    # ------------------------------------------------------------------

    def visitPerformStatement(self, ctx):
        target = None
        try:
            if hasattr(ctx, "procedureName") and ctx.procedureName():
                target = ctx.procedureName().getText()
        except Exception:
            pass

        if not target:
            parts = ctx.getText().split()
            target = parts[1] if len(parts) > 1 else None

        is_loop = (
            (hasattr(ctx, "performUntil") and ctx.performUntil())
            or (hasattr(ctx, "performVarying") and ctx.performVarying())
        )

        perform_node = self._add_node(
            "PERFORM",
            ctx.getText().rstrip("."),
            ctx.start.line,
            {"target": target, "type": "LOOP" if is_loop else "PERFORM"},
        )

        if is_loop:
            if self._current_para:
                self._current_para.has_loop = True
            self._loop_stack.append(perform_node.id)
            try:
                if hasattr(ctx, "performBody"):
                    self.visit(ctx.performBody())
            except Exception:
                pass
            if self._last_id:
                self._add_edge(self._last_id, perform_node.id, "LOOP_BACK")
            self._loop_stack.pop()

        return None

    # ------------------------------------------------------------------
    # Comandos simples — cada um é uma linha, sem chamar visitChildren
    # para não duplicar nós (os filhos não geram nós próprios)
    # ------------------------------------------------------------------

    def _simple(self, kind: str, ctx, extra_meta: dict = None):
        meta = {"type": kind}
        if extra_meta:
            meta.update(extra_meta)
        self._add_node(kind, ctx.getText().rstrip("."), ctx.start.line, meta)
        return None  # NÃO chama visitChildren

    def visitCallStatement(self, ctx):
        try:
            prog = ctx.programName().getText()
        except Exception:
            parts = ctx.getText().split()
            prog = parts[1].strip("'\"") if len(parts) > 1 else "UNKNOWN"
        try:
            using = ctx.usingClause().getText() if hasattr(ctx, "usingClause") and ctx.usingClause() else None
        except Exception:
            using = None
        self._add_node(
            "CALL",
            f"CALL {prog}",
            ctx.start.line,
            {"program": prog, "using": using, "type": "CALL"},
        )
        return None

    def visitMoveStatement(self, ctx):       return self._simple("MOVE", ctx)
    def visitComputeStatement(self, ctx):    return self._simple("COMPUTE", ctx)
    def visitAddStatement(self, ctx):        return self._simple("ADD", ctx)
    def visitSubtractStatement(self, ctx):   return self._simple("SUBTRACT", ctx)
    def visitMultiplyStatement(self, ctx):   return self._simple("MULTIPLY", ctx)
    def visitDivideStatement(self, ctx):     return self._simple("DIVIDE", ctx)
    def visitStringStatement(self, ctx):     return self._simple("STRING", ctx, {"type": "STRING_MANIP"})
    def visitUnstringStatement(self, ctx):   return self._simple("UNSTRING", ctx, {"type": "STRING_MANIP"})
    def visitInspectStatement(self, ctx):    return self._simple("INSPECT", ctx, {"type": "STRING_MANIP"})
    def visitInitializeStatement(self, ctx): return self._simple("INITIALIZE", ctx)
    def visitSetStatement(self, ctx):        return self._simple("SET", ctx)
    def visitOpenStatement(self, ctx):       return self._simple("OPEN", ctx, {"type": "FILE_IO"})
    def visitCloseStatement(self, ctx):      return self._simple("CLOSE", ctx, {"type": "FILE_IO"})
    def visitReadStatement(self, ctx):       return self._simple("READ", ctx, {"type": "FILE_IO"})
    def visitWriteStatement(self, ctx):      return self._simple("WRITE", ctx, {"type": "FILE_IO"})
    def visitRewriteStatement(self, ctx):    return self._simple("REWRITE", ctx, {"type": "FILE_IO"})
    def visitDeleteStatement(self, ctx):     return self._simple("DELETE", ctx, {"type": "FILE_IO"})
    def visitStartStatement(self, ctx):      return self._simple("START", ctx, {"type": "FILE_IO"})
    def visitDisplayStatement(self, ctx):    return self._simple("DISPLAY", ctx)
    def visitAcceptStatement(self, ctx):     return self._simple("ACCEPT", ctx)
    def visitSortStatement(self, ctx):       return self._simple("SORT", ctx)
    def visitMergeStatement(self, ctx):      return self._simple("MERGE", ctx)
    def visitSearchStatement(self, ctx):     return self._simple("SEARCH", ctx)

    def visitGoToStatement(self, ctx):
        self._simple("GOTO", ctx, {"type": "UNCOND_JUMP"})
        self._last_id = None  # fluxo é interrompido
        return None

    def visitExitStatement(self, ctx):
        self._simple("EXIT", ctx, {"type": "RETURN"})
        return None

    def visitStopStatement(self, ctx):
        self._simple("STOP", ctx, {"type": "STOP"})
        self._last_id = None
        return None

    def visitGobackStatement(self, ctx):
        self._simple("GOBACK", ctx, {"type": "RETURN"})
        self._last_id = None
        return None

    def visitContinueStatement(self, ctx):
        return None  # CONTINUE não gera nó — é um no-op

    # Fallback
    def defaultResult(self):
        return None

    def visitChildren(self, ctx):
        result = self.defaultResult()
        if ctx is None:
            return result
        for i in range(ctx.getChildCount()):
            child = ctx.getChild(i)
            if hasattr(child, "accept"):
                child.accept(self)
        return result


# ======================================================================
# 6. Análise pós-visita: padrões semânticos e dead code
# ======================================================================

def detect_patterns(
    nodes: List[GNode],
    edges: List[GEdge],
    var_map: Dict[str, Variable],
    paragraphs: Dict[str, ParagraphProfile],
) -> dict:
    """
    Detecta padrões de negócio comuns no grafo.
    """
    patterns: Dict[str, List[dict]] = defaultdict(list)

    # Conjunto de nós com predecessores (para dead code)
    nodes_with_predecessors: Set[str] = {e.target for e in edges}
    node_ids: Set[str] = {n.id for n in nodes}

    # --- Dead code ---
    dead_nodes = [
        {"id": n.id, "statement": n.statement, "line": n.line, "scope": n.scope}
        for n in nodes
        if n.id not in nodes_with_predecessors
        and n.kind not in ("IF", "EVALUATE")  # nós de entrada válidos
        and n.scope not in ("GLOBAL", "")
    ]
    if dead_nodes:
        patterns["dead_code"] = dead_nodes

    # --- Acumuladores: variáveis que são tanto lidas quanto escritas ---
    accumulator_candidates: Dict[str, int] = defaultdict(int)
    for n in nodes:
        if n.kind in ("ADD", "COMPUTE", "MULTIPLY"):
            rw = set(n.meta.get("reads", [])) & set(n.meta.get("writes", []))
            for v in rw:
                accumulator_candidates[v] += 1
    for var_name, count in accumulator_candidates.items():
        if count >= 1:
            var = var_map.get(var_name)
            patterns["accumulators"].append({
                "variable": var_name,
                "pic": var.pic if var else None,
                "update_count": count,
            })

    # --- Flags 88: variáveis booleanas ---
    for var in var_map.values():
        if var.is_88 and var.condition_values:
            patterns["flags_88"].append({
                "name": var.name,
                "parent": _find_parent(var.name, var_map),
                "values": var.condition_values,
            })

    # --- Loops ---
    loop_nodes = [
        {"id": n.id, "statement": n.statement, "line": n.line, "scope": n.scope}
        for n in nodes
        if n.meta.get("type") == "LOOP"
    ]
    if loop_nodes:
        patterns["loops"] = loop_nodes

    # --- Parágrafos críticos: muitas condições ou calls ---
    for key, prof in paragraphs.items():
        if len(prof.conditions) >= 3:
            patterns["high_complexity_paragraphs"].append({
                "paragraph": key,
                "conditions": len(prof.conditions),
                "calls": len(prof.calls),
                "performs": len(prof.performs),
            })

    # --- Variáveis de linkage (interface) ---
    patterns["linkage_interface"] = [
        {"name": v.name, "pic": v.pic, "level": v.level}
        for key, v in var_map.items()
        # Aproximação: variáveis sem picture e level 01 em linkage
        if v.level == 1
    ]

    return dict(patterns)


def _find_parent(name: str, var_map: Dict[str, Variable]) -> Optional[str]:
    """Encontra o nome da variável pai de um item 88."""
    name_upper = name.upper()
    for var in var_map.values():
        for child in var.children:
            if child.name.upper() == name_upper:
                return var.name
    return None


def detect_dead_code(nodes: List[GNode], edges: List[GEdge]) -> List[dict]:
    """Nós que nunca são alcançados (sem predecessores, exceto o primeiro)."""
    with_pred = {e.target for e in edges}
    first_id = nodes[0].id if nodes else None
    return [
        {"id": n.id, "line": n.line, "statement": n.statement, "scope": n.scope}
        for n in nodes
        if n.id not in with_pred and n.id != first_id
    ]


# ======================================================================
# 7. Sumarizador de grafo para o LLM
# ======================================================================

def summarize_for_llm(
    nodes: List[GNode],
    edges: List[GEdge],
    paragraphs: Dict[str, ParagraphProfile],
) -> List[dict]:
    """
    Converte o grafo em blocos narrativos por parágrafo.
    Reduz drasticamente o número de tokens sem perder semântica.
    """
    edge_map: Dict[str, List[GEdge]] = defaultdict(list)
    for e in edges:
        edge_map[e.source].append(e)

    node_by_id: Dict[str, GNode] = {n.id: n for n in nodes}

    summaries = []
    for scope_key, prof in paragraphs.items():
        para_nodes = [node_by_id[nid] for nid in prof.nodes if nid in node_by_id]

        # Agrupar por tipo
        transformations = [
            {"kind": n.kind, "stmt": n.statement, "line": n.line,
             "reads": n.meta.get("reads", []), "writes": n.meta.get("writes", [])}
            for n in para_nodes
            if n.kind in ("MOVE", "COMPUTE", "ADD", "SUBTRACT", "MULTIPLY",
                          "DIVIDE", "STRING", "UNSTRING", "INSPECT", "INITIALIZE", "SET")
        ]
        conditions = [
            {"stmt": n.statement, "line": n.line,
             "reads": n.meta.get("reads", [])}
            for n in para_nodes
            if n.kind in ("IF", "EVALUATE", "WHEN")
        ]
        file_ops = [
            {"kind": n.kind, "stmt": n.statement, "line": n.line}
            for n in para_nodes
            if n.kind in ("READ", "WRITE", "REWRITE", "DELETE", "OPEN", "CLOSE", "START")
        ]
        calls = [
            {"program": n.meta.get("program"), "stmt": n.statement, "line": n.line}
            for n in para_nodes
            if n.kind == "CALL"
        ]
        performs = [
            {"target": n.meta.get("target"), "line": n.line, "is_loop": n.meta.get("type") == "LOOP"}
            for n in para_nodes
            if n.kind == "PERFORM"
        ]
        gotos = [
            {"stmt": n.statement, "line": n.line}
            for n in para_nodes
            if n.kind == "GOTO"
        ]

        summaries.append({
            "scope": scope_key,
            "paragraph": prof.name,
            "section": prof.section,
            "total_statements": len(para_nodes),
            "variables_read": sorted(prof.reads),
            "variables_written": sorted(prof.writes),
            "transformations": transformations,
            "conditions": conditions,
            "file_operations": file_ops,
            "external_calls": calls,
            "perform_targets": performs,
            "gotos": gotos,
            "has_loop": prof.has_loop,
            "has_exit": prof.has_exit,
        })

    return summaries


# ======================================================================
# 8. Construção do índice de variáveis chave
# ======================================================================

def build_variable_index(data: DataDivision) -> dict:
    """
    Produz um índice enxuto das variáveis mais relevantes para regras de negócio:
    - Flags 88
    - Variáveis de controle de arquivo (FILE-STATUS)
    - Variáveis de linkage (interface entre programas)
    - Variáveis com OCCURS (tabelas)
    - Variáveis com REDEFINES
    """

    def flatten(vars_list: List[Variable], section: str) -> List[dict]:
        result = []

        def rec(v: Variable, parent: Optional[str] = None):
            entry = {
                "name": v.name,
                "level": v.level,
                "pic": v.pic,
                "value": v.value,
                "usage": v.usage,
                "occurs": v.occurs,
                "redefines": v.redefines,
                "is_88": v.is_88,
                "condition_values": v.condition_values,
                "section": section,
                "parent": parent,
                "has_children": len(v.children) > 0,
            }
            result.append(entry)
            for child in v.children:
                rec(child, v.name)

        for var in vars_list:
            rec(var)
        return result

    all_vars = (
        flatten(data.working_storage, "WORKING-STORAGE")
        + flatten(data.linkage, "LINKAGE")
    )

    return {
        "flags_88": [v for v in all_vars if v["is_88"]],
        "tables": [v for v in all_vars if v["occurs"]],
        "redefines": [v for v in all_vars if v["redefines"]],
        "linkage_vars": [v for v in all_vars if v["section"] == "LINKAGE" and v["level"] == 1],
        "all_vars_flat": all_vars,
    }


# ======================================================================
# 9. Prompt otimizado para Opus 4
# ======================================================================

OPUS_SYSTEM_PROMPT = """Você é um especialista em análise de sistemas COBOL com 30 anos de experiência.
Sua missão é extrair 100% das regras de negócio de um programa COBOL a partir de um grafo de controle de fluxo estruturado.

INSTRUÇÕES CRÍTICAS:
1. NÃO omita nenhuma regra de negócio — prefira ser redundante a deixar algo de fora.
2. Para cada parágrafo no campo 'paragraph_summaries', leia:
   - 'conditions': cada IF/EVALUATE é potencialmente uma regra de negócio.
   - 'transformations': MOVE/COMPUTE/ADD revelam cálculos e derivações.
   - 'file_operations': READ/WRITE revelam persistência e validações de I/O.
   - 'external_calls': CALL revela integrações com outros sistemas.
3. Use 'variable_index.flags_88' para interpretar condições booleanas corretamente.
4. Use 'variable_index.tables' para identificar estruturas repetitivas (arrays).
5. Use 'variable_index.linkage_vars' para identificar a interface do programa.
6. Use 'data_division.files' para identificar arquivos acessados e seus modos.
7. Siga o fluxo das arestas em 'graph.edges' para reconstruir sequências e loops.
8. Identifique dead code em 'patterns.dead_code' — pode indicar regras obsoletas.

FORMATO DO DOCUMENTO DE SAÍDA:
Gere um documento técnico com as seguintes seções:

## 1. VISÃO GERAL DO PROGRAMA
- Propósito inferido
- Interface (parâmetros de entrada/saída via LINKAGE)
- Arquivos acessados

## 2. REGRAS DE NEGÓCIO
Para cada regra identificada:
**RN-NNN: [Nome descritivo]**
- Localização: [parágrafo/seção] (linha [N])
- Condição: [quando se aplica]
- Ação: [o que faz]
- Variáveis envolvidas: [lista]
- Observações: [valores 88, tabelas, etc.]

## 3. FLUXO PRINCIPAL
Descrição do fluxo de execução do programa em linguagem natural.

## 4. CÁLCULOS E TRANSFORMAÇÕES
Todas as fórmulas e regras de cálculo identificadas.

## 5. REGRAS DE ARQUIVO E PERSISTÊNCIA
Todas as operações de leitura/escrita com suas condições.

## 6. INTEGRAÇÕES EXTERNAS
Todos os programas chamados via CALL com parâmetros.

## 7. ALERTAS
- Dead code identificado
- Parágrafos de alta complexidade
- Possíveis inconsistências

Seja exaustivo. Um analista de negócios deve conseguir reimplementar o programa lendo apenas este documento."""

OPUS_USER_PROMPT_TEMPLATE = """Analise o seguinte JSON gerado a partir de um programa COBOL e extraia 100% das regras de negócio:

```json
{payload}
```

Gere o documento técnico completo conforme as instruções."""


def build_llm_payload(
    data: DataDivision,
    nodes: List[GNode],
    edges: List[GEdge],
    paragraphs: Dict[str, ParagraphProfile],
    var_map: Dict[str, Variable],
    warnings: List[str],
) -> dict:
    """Monta o payload final para envio ao Opus 4."""
    para_summaries = summarize_for_llm(nodes, edges, paragraphs)
    var_index = build_variable_index(data)
    patterns = detect_patterns(nodes, edges, var_map, paragraphs)

    # Grafo compacto: apenas arestas não-SEQUENCE (as de controle são as mais informativas)
    control_edges = [
        {"from": e.source, "to": e.target, "type": e.relation}
        for e in edges
        if e.relation != "SEQUENCE"
    ]

    payload = {
        "metadata": {
            "parser": "COBOL85-ENHANCED-v2",
            "total_nodes": len(nodes),
            "total_edges": len(edges),
            "total_paragraphs": len(paragraphs),
            "warnings": warnings,
        },
        "data_division": {
            "working_storage": [v.to_dict() for v in data.working_storage],
            "linkage": [v.to_dict() for v in data.linkage],
            "files": [f.to_dict() for f in data.files],
        },
        "variable_index": var_index,
        "paragraph_summaries": para_summaries,
        "patterns": patterns,
        "graph": {
            "control_flow_edges": control_edges,
            "all_nodes": [
                {
                    "id": n.id,
                    "kind": n.kind,
                    "stmt": n.statement,
                    "line": n.line,
                    "scope": n.scope,
                    "reads": n.meta.get("reads", []),
                    "writes": n.meta.get("writes", []),
                }
                for n in nodes
            ],
        },
        "prompts": {
            "system": OPUS_SYSTEM_PROMPT,
            "user": OPUS_USER_PROMPT_TEMPLATE,
        },
    }

    return payload


# ======================================================================
# 10. Função principal de orquestração
# ======================================================================

def cobol_to_graph(
    source: str,
    copy_lib: Optional[Dict[str, str]] = None,
    output_path: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Ponto de entrada principal.

    Args:
        source:      Código-fonte COBOL como string.
        copy_lib:    Dicionário {nome_copybook: conteúdo_string}.
        output_path: Se fornecido, salva o JSON no caminho indicado.

    Returns:
        Dicionário com o payload completo para o LLM.
    """
    warnings: List[str] = []

    # --- Expansão de copybooks ---
    if copy_lib:
        source, copy_warnings = expand_copybooks(source, copy_lib)
        warnings.extend(copy_warnings)

    # --- Parsing ANTLR ---
    # Em produção, substituir pelo parsing real:
    # inp = InputStream(source)
    # lexer = Cobol85Lexer(inp)
    # lexer.removeErrorListeners()
    # stream = CommonTokenStream(lexer)
    # parser = Cobol85Parser(stream)
    # parser.removeErrorListeners()
    # tree = parser.startRule()

    # --- Data Division ---
    # data_listener = DataDivisionListener()
    # walker = ParseTreeWalker()
    # walker.walk(data_listener, tree)
    # data_division = data_listener.data

    # --- Procedure Division ---
    # visitor = CobolRuleVisitor(data_division)
    # visitor.visit(tree)

    # Placeholder para demonstração (remover em produção):
    data_division = DataDivision()
    visitor = CobolRuleVisitor(data_division)

    # --- Montar payload ---
    payload = build_llm_payload(
        data=data_division,
        nodes=visitor.nodes,
        edges=visitor.edges,
        paragraphs=visitor.paragraphs,
        var_map=visitor._var_map,
        warnings=warnings,
    )

    # --- Salvar JSON ---
    if output_path:
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)

    return payload


# ======================================================================
# 11. Utilitário: chamada ao Opus 4 (referência de integração)
# ======================================================================

def call_opus4(payload: dict, api_key: str) -> str:
    """
    Envia o payload para o Claude Opus 4 e retorna o documento gerado.
    Requer: pip install anthropic
    """
    import anthropic  # type: ignore

    # Serializa apenas a parte de dados (sem os prompts embutidos)
    data_payload = {k: v for k, v in payload.items() if k != "prompts"}
    user_message = payload["prompts"]["user"].replace(
        "{payload}", json.dumps(data_payload, ensure_ascii=False, indent=2)
    )

    client = anthropic.Anthropic(api_key=api_key)
    message = client.messages.create(
        model="claude-opus-4-5",      # Opus 4.6 quando disponível via API
        max_tokens=8192,
        system=payload["prompts"]["system"],
        messages=[{"role": "user", "content": user_message}],
    )

    # Extrai o texto da resposta
    return "".join(
        block.text for block in message.content if hasattr(block, "text")
    )


# ======================================================================
# Exemplo de uso
# ======================================================================

if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Uso: python cobol_preprocessor.py <arquivo.cbl> [output.json] [api_key]")
        sys.exit(1)

    cobol_file = sys.argv[1]
    output_json = sys.argv[2] if len(sys.argv) > 2 else "output.json"
    api_key = sys.argv[3] if len(sys.argv) > 3 else None

    with open(cobol_file, encoding="utf-8", errors="replace") as f:
        source_code = f.read()

    # Copybooks: colocar os arquivos .cpy no mesmo diretório e carregar aqui
    # copy_lib = {"MEUBOOK": open("MEUBOOK.cpy").read()}
    copy_lib = {}

    result = cobol_to_graph(source_code, copy_lib, output_json)
    print(f"[OK] Grafo gerado: {result['metadata']['total_nodes']} nós, "
          f"{result['metadata']['total_edges']} arestas, "
          f"{result['metadata']['total_paragraphs']} parágrafos.")
    print(f"[OK] JSON salvo em: {output_json}")

    if api_key:
        print("[...] Enviando para Claude Opus 4...")
        doc = call_opus4(result, api_key)
        doc_path = output_json.replace(".json", "_regras.md")
        with open(doc_path, "w", encoding="utf-8") as f:
            f.write(doc)
        print(f"[OK] Documento de regras salvo em: {doc_path}")
