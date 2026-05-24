"""
Modelos de dados para representar estruturas extraídas de programas COBOL.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional
from enum import Enum


class DivisionType(str, Enum):
    IDENTIFICATION = "IDENTIFICATION"
    ENVIRONMENT = "ENVIRONMENT"
    DATA = "DATA"
    PROCEDURE = "PROCEDURE"


class SectionType(str, Enum):
    FILE = "FILE"
    WORKING_STORAGE = "WORKING-STORAGE"
    LOCAL_STORAGE = "LOCAL-STORAGE"
    LINKAGE = "LINKAGE"


class DecisionType(str, Enum):
    IF = "IF"
    EVALUATE = "EVALUATE"


@dataclass
class Condition88:
    """Cláusula 88 — nomes de condição com valor de negócio semântico."""
    name: str
    values: list[str]
    parent_variable: str
    line: int


@dataclass
class Variable:
    """Variável da DATA DIVISION com metadados enriquecidos."""
    level: int
    name: str
    picture: Optional[str]
    value: Optional[str]
    section: SectionType
    line: int
    conditions_88: list[Condition88] = field(default_factory=list)
    # Preenchido pelo Enricher
    read_in: list[str] = field(default_factory=list)        # parágrafos que leem
    written_in: list[str] = field(default_factory=list)     # parágrafos que escrevem
    is_input_param: bool = False
    is_output_param: bool = False


@dataclass
class DecisionBlock:
    """Bloco IF ou EVALUATE com contexto completo para análise."""
    type: DecisionType
    paragraph: str
    source: str
    line_start: int
    line_end: int
    variables_referenced: dict[str, str]   # nome → PIC
    called_by: list[str] = field(default_factory=list)


@dataclass
class ComputeBlock:
    """Expressão aritmética de negócio."""
    paragraph: str
    target_variable: str
    formula: str
    line: int
    called_by: list[str] = field(default_factory=list)


@dataclass
class ExternalCall:
    """Chamada CALL a sub-rotina externa."""
    program_name: str
    paragraph: str
    parameters: list[str]
    line: int
    called_by: list[str] = field(default_factory=list)


@dataclass
class Paragraph:
    """Parágrafo da PROCEDURE DIVISION."""
    name: str
    section: Optional[str]
    source: str
    line_start: int
    line_end: int
    performs: list[str] = field(default_factory=list)       # o que este parágrafo chama
    performed_by: list[str] = field(default_factory=list)   # quem chama este parágrafo
    decision_blocks: list[DecisionBlock] = field(default_factory=list)
    compute_blocks: list[ComputeBlock] = field(default_factory=list)
    external_calls: list[ExternalCall] = field(default_factory=list)
    is_unreachable: bool = False
    comments: list[str] = field(default_factory=list)


@dataclass
class FileLayout:
    """Layout de arquivo (FD) — estrutura de entrada/saída."""
    name: str
    record_name: str
    fields: list[Variable]
    line: int


@dataclass
class ProgramMetadata:
    """Metadados da IDENTIFICATION DIVISION."""
    program_id: str
    author: Optional[str] = None
    date_written: Optional[str] = None
    remarks: Optional[str] = None


@dataclass
class CobolProgram:
    """Representação completa de um programa COBOL extraído."""
    source_file: str
    metadata: ProgramMetadata
    working_storage: list[Variable] = field(default_factory=list)
    linkage_section: list[Variable] = field(default_factory=list)
    file_layouts: list[FileLayout] = field(default_factory=list)
    paragraphs: list[Paragraph] = field(default_factory=list)
    copybooks_referenced: list[str] = field(default_factory=list)
    raw_lines: list[str] = field(default_factory=list)

    def get_paragraph(self, name: str) -> Optional[Paragraph]:
        name = name.upper().strip()
        return next((p for p in self.paragraphs if p.name == name), None)

    def get_variable(self, name: str) -> Optional[Variable]:
        name = name.upper().strip()
        all_vars = self.working_storage + self.linkage_section
        return next((v for v in all_vars if v.name == name), None)

    @property
    def all_conditions_88(self) -> list[Condition88]:
        result = []
        for v in self.working_storage + self.linkage_section:
            result.extend(v.conditions_88)
        return result

    @property
    def all_decision_blocks(self) -> list[DecisionBlock]:
        result = []
        for p in self.paragraphs:
            result.extend(p.decision_blocks)
        return result

    @property
    def all_external_calls(self) -> list[ExternalCall]:
        result = []
        for p in self.paragraphs:
            result.extend(p.external_calls)
        return result
