"""
Enricher — enriquece o CobolProgram com relacionamentos semânticos.

Constrói:
  - Grafo de chamadas entre parágrafos (performed_by)
  - Mapa de leitura/escrita de variáveis por parágrafo
  - Detecção de parágrafos inalcançáveis (código morto)
  - called_by para DecisionBlocks, ComputeBlocks e ExternalCalls
"""
from __future__ import annotations
import re
from .models import CobolProgram, Paragraph


_RE_MOVE_TARGET = re.compile(
    r'\bMOVE\s+\S+\s+TO\s+([A-Z][A-Z0-9#@$-]*)', re.I
)
_RE_SET_TARGET = re.compile(
    r'\bSET\s+([A-Z][A-Z0-9#@$-]*)\s+TO\b', re.I
)
_RE_ADD_TARGET = re.compile(
    r'\bADD\b.+\bTO\s+([A-Z][A-Z0-9#@$-]*)', re.I
)
_RE_SUBTRACT_TARGET = re.compile(
    r'\bSUBTRACT\b.+\bFROM\s+([A-Z][A-Z0-9#@$-]*)', re.I
)
_RE_MULTIPLY_TARGET = re.compile(
    r'\bMULTIPLY\b.+\bGIVING\s+([A-Z][A-Z0-9#@$-]*)', re.I
)
_RE_DIVIDE_TARGET = re.compile(
    r'\bDIVIDE\b.+\bGIVING\s+([A-Z][A-Z0-9#@$-]*)', re.I
)
_RE_VARREF = re.compile(r'\b([A-Z]{2}[A-Z0-9#@$-]{1,})\b')

_WRITE_PATTERNS = [
    _RE_MOVE_TARGET, _RE_SET_TARGET, _RE_ADD_TARGET,
    _RE_SUBTRACT_TARGET, _RE_MULTIPLY_TARGET, _RE_DIVIDE_TARGET
]


class Enricher:

    def enrich(self, program: CobolProgram) -> CobolProgram:
        """Pipeline de enriquecimento completo."""
        self._build_call_graph(program)
        self._detect_unreachable(program)
        self._enrich_variables(program)
        self._propagate_called_by(program)
        return program

    # ──────────────────────────────────────────────────────────────────────────
    #  1. Grafo de chamadas
    # ──────────────────────────────────────────────────────────────────────────

    def _build_call_graph(self, program: CobolProgram):
        para_names = {p.name for p in program.paragraphs}

        for para in program.paragraphs:
            for target_name in para.performs:
                target = program.get_paragraph(target_name)
                if target and para.name not in target.performed_by:
                    target.performed_by.append(para.name)

    # ──────────────────────────────────────────────────────────────────────────
    #  2. Código morto
    # ──────────────────────────────────────────────────────────────────────────

    def _detect_unreachable(self, program: CobolProgram):
        """
        Marca parágrafos que nunca são chamados (PERFORM) e não são
        o parágrafo de entrada principal.
        """
        if not program.paragraphs:
            return

        # O primeiro parágrafo é sempre alcançável (ponto de entrada)
        entry = program.paragraphs[0].name
        reachable: set[str] = set()
        self._dfs(entry, program, reachable)

        for para in program.paragraphs:
            if para.name not in reachable:
                para.is_unreachable = True

    def _dfs(self, name: str, program: CobolProgram, visited: set[str]):
        if name in visited:
            return
        visited.add(name)
        para = program.get_paragraph(name)
        if para:
            for child in para.performs:
                self._dfs(child, program, visited)

    # ──────────────────────────────────────────────────────────────────────────
    #  3. Mapa de leitura/escrita de variáveis
    # ──────────────────────────────────────────────────────────────────────────

    def _enrich_variables(self, program: CobolProgram):
        all_vars = {v.name for v in program.working_storage + program.linkage_section}

        for para in program.paragraphs:
            source_upper = para.source.upper()

            # Variáveis escritas (alvos de MOVE, COMPUTE, ADD, etc.)
            written: set[str] = set()

            for pattern in _WRITE_PATTERNS:
                for m in pattern.finditer(source_upper):
                    name = m.group(1).upper()
                    if name in all_vars:
                        written.add(name)

            # COMPUTE (alvo explícito)
            for cb in para.compute_blocks:
                if cb.target_variable in all_vars:
                    written.add(cb.target_variable)

            # Variáveis lidas = todas referenciadas - as escritas
            read: set[str] = set()
            for m in _RE_VARREF.finditer(source_upper):
                name = m.group(1).upper()
                if name in all_vars and name not in written:
                    read.add(name)

            # Registra nos objetos de variável
            for var in program.working_storage + program.linkage_section:
                if var.name in written and para.name not in var.written_in:
                    var.written_in.append(para.name)
                if var.name in read and para.name not in var.read_in:
                    var.read_in.append(para.name)

    # ──────────────────────────────────────────────────────────────────────────
    #  4. Propaga called_by para blocos de decisão e computação
    # ──────────────────────────────────────────────────────────────────────────

    def _propagate_called_by(self, program: CobolProgram):
        for para in program.paragraphs:
            for db in para.decision_blocks:
                db.called_by = list(para.performed_by)
            for cb in para.compute_blocks:
                cb.called_by = list(para.performed_by)
            for ec in para.external_calls:
                ec.called_by = list(para.performed_by)
