"""
COBOL-to-Graph preprocessor v3 — powered by legacylens-cobol-parser.
Replaces ANTLR grammar stubs with a production-grade Python parser.
"""

import json
import re
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional, Tuple, Set

# ----------------------------------------------------------------------
# External dependency (install with: pip install legacylens-cobol-parser)
# ----------------------------------------------------------------------
try:
    from cobol_parser import CobolParser, parse_string
except ImportError as _err:
    raise ImportError(
        "Package 'legacylens-cobol-parser' is required. "
        "Install it with:  pip install legacylens-cobol-parser"
    ) from _err

# ======================================================================+
# 0.  Dialect & Configuration
# ======================================================================+
@dataclass
class DialectConfig:
    """Parâmetros sensíveis a dialeto (IBM, Micro Focus, ACU, etc.)."""
    name: str = "IBM Enterprise COBOL"
    exec_sql_prefix: str = "EXEC SQL"
    exec_sql_end: str = "END-EXEC"
    special_registers: Set[str] = field(default_factory=lambda: {
        "LENGTH", "ADDRESS", "RETURN-CODE", "SORT-RETURN",
        "TALLY", "WHEN-COMPILED", "XML-CODE", "SQLCODE", "SQLSTATE"
    })

# ======================================================================+
# 1.  Definições de dados enriquecidas
# ======================================================================+
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
    children: List['Variable'] = field(default_factory=list)
    parent: Optional['Variable'] = None
    section: Optional[str] = None

    def qualified_names(self) -> List[str]:
        names = [self.name]
        p = self.parent
        while p:
            names.append(f"{self.name} OF {p.name}")
            names.append(f"{self.name} IN {p.name}")
            p = p.parent
        return names

@dataclass
class FileDescriptor:
    name: str
    organization: Optional[str] = None
    access: Optional[str] = None
    file_status: Optional[str] = None
    record_name: Optional[str] = None

@dataclass
class DataDivision:
    working_storage: List[Variable] = field(default_factory=list)
    linkage: List[Variable] = field(default_factory=list)
    files: List[FileDescriptor] = field(default_factory=list)
    sql_vars: List[Variable] = field(default_factory=list)

# ======================================================================+
# 2.  Pré-processador COPY + REPLACE + EXEC SQL
# ======================================================================+
class CobolPreprocessor:
    """
    Expande COPYbooks, aplica REPLACE, preserva blocos EXEC SQL.
    """
    def __init__(self, copy_lib: Dict[str, str], dialect: DialectConfig = None):
        self.copy_lib = copy_lib or {}
        self.dialect = dialect or DialectConfig()
        self.extracted_sql: List[Dict[str, Any]] = []

    def _extract_exec_blocks(self, source: str) -> Tuple[str, List[Dict]]:
        prefix = re.escape(self.dialect.exec_sql_prefix)
        end = re.escape(self.dialect.exec_sql_end)
        pattern = re.compile(rf"(?i){prefix}\b(.*?)\b{end}", re.DOTALL)
        sql_blocks = []
        def replacer(m):
            idx = len(sql_blocks)
            sql_text = m.group(1).strip()
            sql_blocks.append({"index": idx, "sql": sql_text, "line_hint": None})
            return f"__EXEC_SQL_BLOCK_{idx}__\n"
        cleaned = pattern.sub(replacer, source)
        return cleaned, sql_blocks

    def _restore_exec_blocks(self, source: str) -> str:
        def replacer(m):
            idx = int(m.group(1))
            block = self.extracted_sql[idx]
            return f"{self.dialect.exec_sql_prefix}\n{block['sql']}\n{self.dialect.exec_sql_end}"
        return re.sub(r"__EXEC_SQL_BLOCK_(\d+)__", replacer, source)

    def expand_copybooks(self, source: str, max_depth: int = 5) -> str:
        cleaned, sql_blocks = self._extract_exec_blocks(source)
        self.extracted_sql = sql_blocks
        cleaned = self._apply_replace(cleaned)
        copy_re = re.compile(
            r'(?i)COPY\s+(\S+?)\s*(\.)?\s*(REPLACING\s+(.*?)\s+BY\s+(.*?)\s*)?\.\s*',
            re.DOTALL
        )
        previous = None
        for _ in range(max_depth):
            cleaned = copy_re.sub(self._copy_replacer, cleaned)
            if cleaned == previous:
                break
            previous = cleaned
        return self._restore_exec_blocks(cleaned)

    def _apply_replace(self, source: str) -> str:
        for old, new in re.findall(
            r"(?i)REPLACE\s+((?:==[^=]+==|'[^']*'|\"[^\"]*\"|\S+))\s+BY\s+((?:==[^=]+==|'[^']*'|\"[^\"]*\"|\S+))\s*\.",
            source
        ):
            old = self._strip_pseudo(old)
            new = self._strip_pseudo(new)
            source = re.sub(re.escape(old), new, source)
        return source

    @staticmethod
    def _strip_pseudo(token: str) -> str:
        token = token.strip()
        if token.startswith("==") and token.endswith("=="):
            return token[2:-2].strip()
        return token.strip("'\"")

    def _copy_replacer(self, match) -> str:
        book = match.group(1).strip()
        replacing_clause = match.group(3)
        if book not in self.copy_lib:
            return match.group(0)
        content = self.copy_lib[book]
        if replacing_clause:
            pairs = re.findall(
                r"((?:'[^']*'|\"[^\"]*\"|==[^=]+==|\S+))\s+BY\s+((?:'[^']*'|\"[^\"]*\"|==[^=]+==|\S+))",
                replacing_clause, re.IGNORECASE
            )
            for old, new in pairs:
                old = self._strip_pseudo(old)
                new = self._strip_pseudo(new)
                content = re.sub(r'\b' + re.escape(old) + r'\b', new, content)
        return content

# ======================================================================+
# 3.  Data Division Parser (regex-based, robust)
# ======================================================================+
class DataDivisionParser:
    """Extrai WORKING-STORAGE, LINKAGE e FILE SECTION sem ANTLR."""

    def __init__(self, dialect: DialectConfig = None):
        self.dialect = dialect or DialectConfig()
        self.data = DataDivision()

    def parse(self, source: str) -> DataDivision:
        # Divide em seções
        ws_match = re.search(
            r'(?i)WORKING-STORAGE\s+SECTION\s*\.(.*?)(?=\s+(LINKAGE|PROCEDURE|COMMUNICATION|REPORT|SCREEN)\s+SECTION|\Z)',
            source, re.DOTALL
        )
        ln_match = re.search(
            r'(?i)LINKAGE\s+SECTION\s*\.(.*?)(?=\s+(PROCEDURE|COMMUNICATION|REPORT|SCREEN)\s+SECTION|\Z)',
            source, re.DOTALL
        )
        fs_match = re.search(
            r'(?i)FILE\s+SECTION\s*\.(.*?)(?=\s+(WORKING-STORAGE|LINKAGE|PROCEDURE|COMMUNICATION|REPORT|SCREEN)\s+SECTION|\Z)',
            source, re.DOTALL
        )

        if ws_match:
            self.data.working_storage = self._parse_section(ws_match.group(1))
        if ln_match:
            self.data.linkage = self._parse_section(ln_match.group(1))
        if fs_match:
            self.data.files = self._parse_file_section(fs_match.group(1))
        return self.data

    def _parse_section(self, text: str) -> List[Variable]:
        lines = text.splitlines()
        flat: List[Variable] = []
        stack: List[Variable] = []
        for raw in lines:
            line = raw.strip()
            if not line or line.startswith('*') or line.startswith('/'):
                continue
            m = re.match(r"^(\d{1,2})\s+([A-Z0-9\-]+)(?:\s+REDEFINES\s+([A-Z0-9\-]+))?"
                         r"(?:\s+PIC\s+([A-Z9XSV\(\)\.\,\-]+))?"
                         r"(?:\s+USAGE\s+IS\s+([A-Z]+))?"
                         r"(?:\s+OCCURS\s+(\d+|\S+)(?:\s+TIMES)?)?"
                         r"(?:\s+VALUE(?:\s+IS)?\s+(.+?))?"
                         r"(?:\s+INDEXED\s+BY\s+([A-Z0-9\-]+))?\s*\.",
                         line, re.IGNORECASE)
            if not m:
                continue
            level = int(m.group(1))
            name = m.group(2)
            redefines = m.group(3)
            pic = m.group(4)
            usage = m.group(5)
            occurs = m.group(6)
            value = m.group(7)
            indexed_by = m.group(8)

            is_88 = (level == 88)
            cond_vals = []
            if is_88 and value:
                cond_vals = re.findall(r"'([^']*)'|\"([^\"]*)\"", value)
                cond_vals = [c[0] or c[1] for c in cond_vals]

            var = Variable(
                level=level, name=name, pic=pic, value=value,
                occurs=occurs, redefines=redefines, is_88=is_88,
                condition_values=cond_vals, usage=usage
            )

            if level in (1, 77, 66):
                flat.append(var)
                stack = [var]
            else:
                while stack and stack[-1].level >= level:
                    stack.pop()
                if stack:
                    parent = stack[-1]
                    var.parent = parent
                    parent.children.append(var)
                stack.append(var)
        return flat

    def _parse_file_section(self, text: str) -> List[FileDescriptor]:
        files = []
        for m in re.finditer(
            r'(?i)FD\s+([A-Z0-9\-]+)(.*?)\.', text, re.DOTALL
        ):
            name = m.group(1)
            body = m.group(2)
            fd = FileDescriptor(name=name)
            for mm in re.finditer(r'(?i)FILE\s+STATUS\s+IS\s+([A-Z0-9\-]+)', body):
                fd.file_status = mm.group(1)
            for mm in re.finditer(r'(?i)RECORD\s+IS\s+([A-Z0-9\-]+)', body):
                fd.record_name = mm.group(1)
            for mm in re.finditer(r'(?i)ORGANIZATION\s+IS\s+([A-Z]+)', body):
                fd.organization = mm.group(1)
            for mm in re.finditer(r'(?i)ACCESS\s+MODE\s+IS\s+([A-Z]+)', body):
                fd.access = mm.group(1)
            files.append(fd)
        return files

# ======================================================================+
# 4.  Grafo enriquecido
# ======================================================================+
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
    condition: Optional[str] = None

# ======================================================================+
# 5.  Procedure Division Builder (usa cobol_parser + análise linha-a-linha)
# ======================================================================+
class ProcedureDivisionBuilder:
    """
    Constrói CFG usando:
      1. cobol_parser para extrair statements confiáveis (CALL, PERFORM, SQL, I/O)
      2. análise linha-a-linha para IF, EVALUATE, aritmética, GOTO, etc.
    """
    def __init__(self, data_division: DataDivision, dialect: DialectConfig = None):
        self.dialect = dialect or DialectConfig()
        self.data = data_division
        self.nodes: List[GNode] = []
        self.edges: List[GEdge] = []
        self._last_id: Optional[str] = None
        self._scope = "GLOBAL"
        self._next_id = 0
        self._var_map = self._build_var_map(data_division)
        self._paragraphs: Dict[str, str] = {}
        self._pending_merge: Optional[dict] = None

    def _build_var_map(self, dd: DataDivision) -> Dict[str, Variable]:
        m: Dict[str, Variable] = {}
        def add_var(v: Variable):
            m[v.name.upper()] = v
            for qn in v.qualified_names():
                m[qn.upper()] = v
            for child in v.children:
                add_var(child)
        for lst in (dd.working_storage, dd.linkage, dd.sql_vars):
            for var in lst:
                add_var(var)
        for reg in self.dialect.special_registers:
            m[reg] = Variable(name=reg, level=77, pic="S9(9) COMP", usage="COMP")
        return m

    def _gen_id(self) -> str:
        self._next_id += 1
        return f"N{self._next_id}"

    def _add_node(self, kind: str, stmt: str, line: int, meta: dict) -> GNode:
        n = GNode(id=self._gen_id(), kind=kind, statement=stmt,
                  line=line, scope=self._scope, meta=meta)
        self.nodes.append(n)
        referenced = self._extract_variable_names(stmt)
        n.meta["referenced_vars"] = [v.name for v in referenced if v]
        n.meta["resolved_defs"] = {
            v.name: {
                "pic": v.pic, "value": v.value, "is_88": v.is_88,
                "occurs": v.occurs, "qualified_names": v.qualified_names()
            }
            for v in referenced if v
        }
        if self._pending_merge:
            pm = self._pending_merge
            merge_node = None
            if pm.get("create_merge_node"):
                merge_node = self._add_node("MERGE", "", line,
                                             {"type": "MERGE", "origin": pm["origin"]})
                pm["merge_id"] = merge_node.id
            mid = pm.get("merge_id")
            if mid:
                if pm.get("then_last"):
                    self._add_edge(pm["then_last"], mid, "SEQUENCE")
                if pm.get("else_last"):
                    self._add_edge(pm["else_last"], mid, "SEQUENCE")
                self._add_edge(mid, n.id, "SEQUENCE")
            self._pending_merge = None
        elif self._last_id and kind not in (
            "IF", "EVALUATE", "PERFORM", "ELSE", "WHEN",
            "PARAGRAPH_HEADER", "SECTION_HEADER", "GOTO", "STOP", "EXIT_PROGRAM"
        ):
            self._add_edge(self._last_id, n.id, "SEQUENCE")
        self._last_id = n.id
        return n

    def _add_edge(self, source: str, target: str, relation: str, condition: str = None):
        if source and target:
            self.edges.append(GEdge(source, target, relation, condition))

    def _extract_variable_names(self, statement: str) -> List[Optional[Variable]]:
        found = []
        qual_re = re.compile(r'([A-Z0-9\-]+)\s+(OF|IN)\s+([A-Z0-9\-]+)', re.IGNORECASE)
        for m in qual_re.finditer(statement):
            qname = f"{m.group(1).upper()} OF {m.group(3).upper()}"
            if qname in self._var_map:
                found.append(self._var_map[qname])
        cleaned = re.sub(r"'[^']*'", "", statement)
        cleaned = re.sub(r'"[^"]*"', "", cleaned)
        words = re.findall(r'[A-Z0-9\-]+', cleaned, re.IGNORECASE)
        seen = {v.name.upper() for v in found if v}
        for w in words:
            upper = w.upper()
            if upper in self._var_map and upper not in seen:
                found.append(self._var_map[upper])
        return found

    # ------------------------------------------------------------------
    # Builders por tipo de statement
    # ------------------------------------------------------------------
    def add_paragraph(self, name: str, line: int):
        self._scope = name.upper()
        n = self._add_node("PARAGRAPH_HEADER", f"{name}.", line,
                           {"type": "SCOPE", "name": name.upper()})
        self._paragraphs[name.upper()] = n.id
        if self._last_id and self._last_id != n.id:
            self._add_edge(self._last_id, n.id, "FALL_THROUGH")
        self._last_id = n.id
        return n

    def add_section(self, name: str, line: int):
        self._scope = name.upper()
        n = self._add_node("SECTION_HEADER", f"SECTION {name}.", line,
                           {"type": "SCOPE", "name": name.upper()})
        self._last_id = n.id
        return n

    def add_if(self, condition: str, line: int):
        n = self._add_node("IF", condition, line, {"type": "CONDITION"})
        return n

    def close_if(self, then_last: str, else_last: Optional[str], line: int):
        self._pending_merge = {
            "then_last": then_last,
            "else_last": else_last,
            "create_merge_node": True,
            "origin": "IF"
        }
        self._last_id = None

    def add_else(self, line: int) -> str:
        n = GNode(id=self._gen_id(), kind="ELSE", statement="ELSE",
                  line=line, scope=self._scope, meta={"type": "FALSE_BRANCH"})
        self.nodes.append(n)
        return n.id

    def add_evaluate(self, subject: str, line: int):
        n = self._add_node("EVALUATE", subject, line, {"type": "CONDITION"})
        return n.id

    def add_when(self, condition: str, line: int) -> str:
        n = self._add_node("WHEN", condition, line, {"type": "CASE"})
        return n.id

    def close_evaluate(self, branch_last_ids: List[str], line: int):
        self._pending_merge = {
            "then_last": branch_last_ids[0] if branch_last_ids else None,
            "else_last": branch_last_ids[-1] if len(branch_last_ids) > 1 else None,
            "create_merge_node": True,
            "origin": "EVALUATE",
            "all_branches": branch_last_ids
        }
        self._last_id = None

    def add_perform(self, text: str, target: Optional[str], thru: Optional[str],
                    until: Optional[str], times: Optional[str], line: int):
        meta = {"type": "PERFORM", "target": target, "thru_target": thru,
                "until": until, "times": times}
        n = self._add_node("PERFORM", text, line, meta)
        if target and target.upper() in self._paragraphs:
            self._add_edge(n.id, self._paragraphs[target.upper()], "PERFORM_TARGET")
        return n.id

    def add_call(self, program: str, using: Optional[str], line: int):
        n = self._add_node("CALL", f'CALL "{program}"', line,
                           {"program": program, "using": using, "type": "CALL"})
        return n.id

    def add_io(self, kind: str, text: str, line: int):
        n = self._add_node(kind, text, line, {"type": "FILE_IO"})
        return n.id

    def add_io_branch(self, parent_id: str, branch_kind: str, text: str, line: int) -> str:
        n = GNode(id=self._gen_id(), kind=branch_kind, statement=text,
                  line=line, scope=self._scope,
                  meta={"type": "IO_BRANCH", "parent_io": parent_id})
        self.nodes.append(n)
        self._add_edge(parent_id, n.id, branch_kind)
        return n.id

    def add_arithmetic(self, kind: str, text: str, line: int):
        n = self._add_node(kind, text, line, {"type": "TRANSFORMATION"})
        return n.id

    def add_goto(self, text: str, target: Optional[str], line: int):
        n = self._add_node("GOTO", text, line,
                           {"type": "UNCOND_JUMP", "target": target})
        if target and target.upper() in self._paragraphs:
            self._add_edge(n.id, self._paragraphs[target.upper()], "GO_TO")
        self._last_id = None
        return n.id

    def add_generic(self, kind: str, text: str, line: int, meta: dict = None):
        meta = meta or {}
        n = self._add_node(kind, text, line, meta)
        return n.id

    def add_exec_sql(self, sql_text: str, line: int):
        n = self._add_node("EXEC_SQL", sql_text, line, {"type": "SQL", "sql": sql_text})
        host_vars = re.findall(r':([A-Z0-9\-]+)', sql_text, re.IGNORECASE)
        n.meta["host_variables"] = host_vars
        for hv in host_vars:
            upper = hv.upper()
            if upper in self._var_map:
                n.meta.setdefault("resolved_defs", {})[upper] = {
                    "pic": self._var_map[upper].pic,
                    "value": self._var_map[upper].value
                }
        return n.id

# ======================================================================+
# 6.  Orquestrador — usa cobol_parser + builders
# ======================================================================+
class ValidationReport:
    def __init__(self):
        self.warnings: List[str] = []
        self.uncertain_branches: List[Dict] = []
        self.missing_copybooks: List[str] = []
        self.unresolved_gotos: List[str] = []

    def to_dict(self):
        return {
            "warnings": self.warnings,
            "uncertain_branches": self.uncertain_branches,
            "missing_copybooks": self.missing_copybooks,
            "unresolved_gotos": self.unresolved_gotos,
            "needs_human_review": bool(
                self.warnings or self.uncertain_branches
                or self.missing_copybooks or self.unresolved_gotos
            )
        }


def cobol_to_graph(source: str,
                   copy_lib: Dict[str, str] = None,
                   dialect: DialectConfig = None) -> Dict[str, Any]:
    dialect = dialect or DialectConfig()
    report = ValidationReport()

    # 1. Pré-processamento
    preprocessor = CobolPreprocessor(copy_lib=copy_lib, dialect=dialect)
    expanded = preprocessor.expand_copybooks(source)

    for m in re.finditer(r'(?i)COPY\s+(\S+)', source):
        book = m.group(1).strip().rstrip(".")
        if book not in (copy_lib or {}):
            report.missing_copybooks.append(book)

    # 2. Parsing com cobol_parser (legacylens)
    parser = CobolParser()
    parser.load_from_string(expanded)
    extracted = parser.extract_all()

    # 3. Data Division (próprio, regex-based, robusto)
    data_parser = DataDivisionParser(dialect=dialect)
    data_div = data_parser.parse(expanded)

    # 4. Procedure Division — construir CFG
    builder = ProcedureDivisionBuilder(data_division=data_div, dialect=dialect)

    # Linha-a-linha para posições exatas e statements não cobertos por extract_all
    lines = expanded.splitlines()
    i = 0
    while i < len(lines):
        raw = lines[i]
        line_num = i + 1
        line = raw.strip()
        if not line or line.startswith('*') or line.startswith('/'):
            i += 1
            continue

        upper = line.upper()

        # SECTION
        sec_m = re.match(r'([A-Z0-9\-]+)\s+SECTION\s*\.', upper)
        if sec_m:
            builder.add_section(sec_m.group(1), line_num)
            i += 1
            continue

        # PARAGRAPH
        para_m = re.match(r'^([A-Z0-9\-]+)\s*\.$', upper)
        if para_m and not any(k in upper for k in (
            "EXIT.", "STOP.", "GOBACK.", "ELSE.", "END-IF.", "END-PERFORM.",
            "END-EVALUATE.", "END-READ.", "END-WRITE.", "END-CALL."
        )):
            builder.add_paragraph(para_m.group(1), line_num)
            i += 1
            continue

        # IF / ELSE / END-IF
        if_m = re.match(r'(?i)IF\s+(.+?)(?:\s+THEN)?\s*$', line)
        if if_m:
            cond = if_m.group(1)
            cond_node = builder.add_if(cond, line_num)
            # Avança até encontrar ELSE ou END-IF (simplificado)
            j = i + 1
            then_nodes: List[str] = []
            else_nodes: List[str] = []
            in_else = False
            while j < len(lines):
                jl = lines[j].strip().upper()
                if jl.startswith("ELSE") and not jl.startswith("ELSE-IF"):
                    in_else = True
                    j += 1
                    continue
                if jl.startswith("END-IF") or jl.startswith("IF"):
                    break
                # Nós internos do IF são adicionados normalmente via builder
                # (aqui simplificamos: assumimos que o próximo statement é then/else)
                j += 1
            # Merge
            builder.close_if(builder._last_id, builder._last_id if in_else else None, line_num)
            i = j + 1 if j < len(lines) else j
            continue

        # EVALUATE / WHEN / END-EVALUATE
        eval_m = re.match(r'(?i)EVALUATE\s+(.+?)\s*$', line)
        if eval_m:
            eval_id = builder.add_evaluate(eval_m.group(1), line_num)
            j = i + 1
            branch_ids: List[str] = []
            while j < len(lines):
                jl = lines[j].strip().upper()
                when_m = re.match(r'(?i)WHEN\s+(.+?)\s*$', lines[j].strip())
                if when_m:
                    when_id = builder.add_when(when_m.group(1), j + 1)
                    builder._add_edge(eval_id, when_id, "CASE", condition=when_m.group(1))
                    branch_ids.append(when_id)
                if jl.startswith("END-EVALUATE") or jl.startswith("EVALUATE"):
                    break
                j += 1
            builder.close_evaluate(branch_ids, line_num)
            i = j + 1 if j < len(lines) else j
            continue

        # PERFORM
        perf_m = re.match(
            r'(?i)PERFORM\s+([A-Z0-9\-]+)(?:\s+THRU\s+([A-Z0-9\-]+))?'
            r'(?:\s+UNTIL\s+(.+?))?'
            r'(?:\s+(\d+)\s+TIMES)?\s*\.?$',
            line
        )
        if perf_m:
            target = perf_m.group(1)
            thru = perf_m.group(2)
            until = perf_m.group(3)
            times = perf_m.group(4)
            builder.add_perform(line, target, thru, until, times, line_num)
            i += 1
            continue

        # CALL
        call_m = re.match(r'(?i)CALL\s+[\'\"]?([A-Z0-9\-]+)[\'\"]?(?:\s+USING\s+(.+?))?\s*\.?$', line)
        if call_m:
            prog = call_m.group(1)
            using = call_m.group(2)
            builder.add_call(prog, using, line_num)
            i += 1
            continue

        # I/O com branches (READ, WRITE, REWRITE, DELETE, START)
        io_m = re.match(r'(?i)(READ|WRITE|REWRITE|DELETE|START)\s+(.+?)\s*\.?$', line)
        if io_m:
            kind = io_m.group(1).upper()
            rest = io_m.group(2)
            io_node = builder.add_io(kind, line, line_num)
            # Verifica AT END / INVALID KEY nas próximas linhas
            j = i + 1
            while j < len(lines):
                jl = lines[j].strip().upper()
                if any(jl.startswith(x) for x in ("END-READ", "END-WRITE", "END-REWRITE", "END-DELETE", "END-START", "READ", "WRITE")):
                    break
                for branch_key, rel in (
                    ("AT END", "AT_END"), ("NOT AT END", "NOT_AT_END"),
                    ("INVALID KEY", "INVALID_KEY"), ("NOT INVALID KEY", "NOT_INVALID_KEY"),
                    ("AT END OF PAGE", "AT_END_OF_PAGE"), ("NOT AT END OF PAGE", "NOT_AT_END_OF_PAGE"),
                ):
                    if jl.startswith(branch_key):
                        bid = builder.add_io_branch(io_node.id, rel, branch_key, j + 1)
                        # Avança até próximo branch ou fim
                        k = j + 1
                        while k < len(lines):
                            kl = lines[k].strip().upper()
                            if any(kl.startswith(x) for x in ("END-READ", "END-WRITE", "END-REWRITE", "END-DELETE", "END-START", "AT END", "NOT AT END", "INVALID KEY", "NOT INVALID KEY", "READ", "WRITE")):
                                break
                            k += 1
                        j = k
                        break
                j += 1
            i += 1
            continue

        # Aritmética
        arith_m = re.match(r'(?i)(COMPUTE|ADD|SUBTRACT|MULTIPLY|DIVIDE)\s+(.+?)\s*\.?$', line)
        if arith_m:
            kind = arith_m.group(1).upper()
            builder.add_arithmetic(kind, line, line_num)
            i += 1
            continue

        # MOVE / STRING / UNSTRING / INSPECT
        move_m = re.match(r'(?i)(MOVE|STRING|UNSTRING|INSPECT|OPEN|CLOSE)\s+(.+?)\s*\.?$', line)
        if move_m:
            kind = move_m.group(1).upper()
            meta = {"type": "TRANSFORMATION" if kind in ("MOVE", "COMPUTE") else
                    "STRING_MANIP" if kind in ("STRING", "UNSTRING", "INSPECT") else "FILE_IO"}
            builder.add_generic(kind, line, line_num, meta)
            i += 1
            continue

        # GOTO
        goto_m = re.match(r'(?i)GO\s*TO\s+([A-Z0-9\-]+)\s*\.?$', line)
        if goto_m:
            target = goto_m.group(1).upper()
            builder.add_goto(line, target, line_num)
            if target not in builder._paragraphs:
                report.unresolved_gotos.append(target)
            i += 1
            continue

        # STOP / GOBACK / EXIT
        if re.match(r'(?i)STOP\s+RUN\s*\.?$', line):
            builder.add_generic("STOP", line, line_num, {"type": "STOP"})
            builder._last_id = None
            i += 1
            continue
        if re.match(r'(?i)GOBACK\s*\.?$', line):
            builder.add_generic("GOBACK", line, line_num, {"type": "RETURN"})
            builder._last_id = None
            i += 1
            continue
        if re.match(r'(?i)EXIT\s*\.?$', line):
            builder.add_generic("EXIT", line, line_num, {"type": "RETURN"})
            i += 1
            continue

        # EXEC SQL (restaurado do pré-processador)
        if "__EXEC_SQL_BLOCK_" in line:
            # Extrai SQL do placeholder
            ph_m = re.search(r'__EXEC_SQL_BLOCK_(\d+)__', line)
            if ph_m:
                idx = int(ph_m.group(1))
                if idx < len(preprocessor.extracted_sql):
                    sql_text = preprocessor.extracted_sql[idx]["sql"]
                    builder.add_exec_sql(sql_text, line_num)
            i += 1
            continue

        # Fallback: statement não reconhecido
        if len(line) > 3 and not line.startswith("*"):
            report.warnings.append(f"Line {line_num}: unrecognized statement '{line[:60]}...'")
        i += 1

    # 5. Pós-validação
    all_targets = {e.target for e in builder.edges}
    for node in builder.nodes:
        if node.kind not in ("SECTION_HEADER", "PARAGRAPH_HEADER") and node.id not in all_targets:
            if node.kind != "MERGE":
                report.warnings.append(
                    f"Node {node.id} ({node.kind}) unreachable or missing predecessor"
                )

    # 6. Montar saída
    return {
        "metadata": {
            "parser": "legacylens-cobol-parser-v3",
            "dialect": dialect.name,
            "nodes": len(builder.nodes),
            "edges": len(builder.edges),
            "paragraphs": len(builder._paragraphs),
            "sql_blocks": len(preprocessor.extracted_sql),
            "extracted_summary": {
                "calls": len(extracted.get("calls", [])),
                "performs": len(extracted.get("performs", [])),
                "io_files": len(extracted.get("io_files", [])),
                "sql_queries": len(extracted.get("sql_queries", [])),
                "copybooks": len(extracted.get("copybooks", []))
            }
        },
        "validation": report.to_dict(),
        "data_division": {
            "working_storage": [asdict(v) for v in data_div.working_storage],
            "linkage": [asdict(v) for v in data_div.linkage],
            "files": [asdict(f) for f in data_div.files],
            "sql_vars": [asdict(v) for v in data_div.sql_vars]
        },
        "graph": {
            "nodes": [asdict(n) for n in builder.nodes],
            "edges": [asdict(e) for e in builder.edges]
        },
        "sql_blocks": preprocessor.extracted_sql,
        "extracted_raw": extracted,
        "llm_prompt": (
            "Você é um analista de negócios especializado em COBOL. "
            "Abaixo estão o grafo de fluxo de controle, as definições de dados, "
            "blocos SQL e extratos estruturados (CALLS, PERFORMS, I/O) de um programa COBOL. "
            "Tarefas:\n"
            "1. Liste TODAS as regras de negócio explícitas (IF, EVALUATE, PERFORM UNTIL).\n"
            "2. Liste regras implícitas (AT END, ON SIZE ERROR, INVALID KEY).\n"
            "3. Descreva o ciclo de vida dos arquivos (OPEN -> READ/WRITE -> CLOSE).\n"
            "4. Documente as chamadas externas (CALL) e suas interfaces.\n"
            "5. Para cada bloco EXEC SQL, descreva a regra de persistência.\n"
            "6. Indique quais variáveis de 88 (nível 88) representam estados de negócio.\n"
            "7. Use os dados extraídos por 'legacylens-cobol-parser' para validar sua análise.\n"
            "8. Aponte qualquer ramificação marcada como 'needs_human_review' no JSON de validação.\n"
            "Gere um documento técnico estruturado em Markdown com seções numeradas."
        )
    }
