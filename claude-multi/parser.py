"""
Parser principal do COBOL.

Extrai todas as estruturas relevantes para análise de regras de negócio:
  - IDENTIFICATION DIVISION → metadados
  - DATA DIVISION → variáveis, cláusulas 88, layouts de arquivo
  - PROCEDURE DIVISION → parágrafos, IFs, EVALUATEs, COMPUTEs, CALLs
"""
from __future__ import annotations
import re
from .models import (
    CobolProgram, ProgramMetadata, Variable, Condition88,
    FileLayout, Paragraph, DecisionBlock, ComputeBlock, ExternalCall,
    SectionType, DecisionType
)
from .preprocessor import PreprocessedLine


# ──────────────────────────────────────────────────────────────────────────────
#  Regex centralizadas
# ──────────────────────────────────────────────────────────────────────────────
_RE_PROGRAM_ID  = re.compile(r'PROGRAM-ID\s*[.\s]+([A-Z0-9#@$-]+)', re.I)
_RE_AUTHOR      = re.compile(r'AUTHOR\s*[.\s]+(.*)', re.I)
_RE_DATE        = re.compile(r'DATE-WRITTEN\s*[.\s]+(.*)', re.I)
_RE_REMARKS     = re.compile(r'REMARKS\s*[.\s]+(.*)', re.I)
_RE_DIVISION    = re.compile(r'\b(IDENTIFICATION|ENVIRONMENT|DATA|PROCEDURE)\s+DIVISION\b', re.I)
_RE_SECTION     = re.compile(
    r'\b(FILE|WORKING-STORAGE|LOCAL-STORAGE|LINKAGE)\s+SECTION\b', re.I
)
_RE_FD          = re.compile(r'^FD\s+([A-Z0-9#@$-]+)', re.I)
_RE_VARIABLE    = re.compile(
    r'^(\d{1,2})\s+([A-Z0-9#@$-]+)(.*)', re.I
)
_RE_PIC         = re.compile(r'\bPIC(?:TURE)?\s+IS\s+(\S+)|\bPIC(?:TURE)?\s+(\S+)', re.I)
_RE_VALUE       = re.compile(r'\bVALUE\s+(?:IS\s+)?(.+?)(?:\.|$)', re.I)
_RE_PARA        = re.compile(r'^([A-Z0-9#@$-]{2,})\s*(?:SECTION)?\s*\.\s*$', re.I)
_RE_PERFORM     = re.compile(r'\bPERFORM\s+([A-Z0-9#@$-]+)', re.I)
_RE_CALL        = re.compile(r'\bCALL\s+[\'"]?([A-Z0-9#@$-]+)[\'"]?', re.I)
_RE_USING       = re.compile(r'\bUSING\b(.+)', re.I)
_RE_COMPUTE     = re.compile(r'\bCOMPUTE\s+([A-Z0-9#@$-]+)\s*=\s*(.+)', re.I)
_RE_MOVE        = re.compile(r'\bMOVE\b', re.I)
_RE_SET         = re.compile(r'\bSET\b', re.I)
_RE_VARREF      = re.compile(r'\b([A-Z]{2}[A-Z0-9#@$-]{1,})\b')
_RE_SECTION_DEF = re.compile(r'^([A-Z0-9#@$-]+)\s+SECTION\s*\.', re.I)


class CobolParser:

    def parse(
        self,
        lines: list[PreprocessedLine],
        source_file: str,
        copybooks: list[str]
    ) -> CobolProgram:

        program = CobolProgram(
            source_file=source_file,
            metadata=ProgramMetadata(program_id='UNKNOWN'),
            copybooks_referenced=copybooks,
            raw_lines=[l.content for l in lines if not l.is_comment]
        )

        # Captura comentários por parágrafo (para uso posterior)
        comments_buffer: list[str] = []
        para_comments: dict[str, list[str]] = {}

        current_division = None
        current_section = None
        current_fd: str | None = None
        current_para: Paragraph | None = None
        current_section_name: str | None = None

        # Buffers para blocos multi-linha
        block_buffer: list[str] = []
        block_type: str | None = None
        block_start: int = 0
        paren_depth = 0

        # Para variáveis: acumula linhas de uma declaração multi-linha
        var_buffer: str = ''
        var_start_line: int = 0

        i = 0
        while i < len(lines):
            line = lines[i]

            # ── Comentários ───────────────────────────────────────────────
            if line.is_comment:
                txt = line.comment_text.strip()
                if txt:
                    comments_buffer.append(txt)
                i += 1
                continue

            content = line.content.strip()
            if not content:
                i += 1
                continue

            upper = content.upper()

            # ── Divisões ──────────────────────────────────────────────────
            m = _RE_DIVISION.search(upper)
            if m:
                current_division = m.group(1).upper()
                current_section = None
                current_fd = None
                var_buffer = ''
                comments_buffer.clear()
                i += 1
                continue

            # ── Seções ────────────────────────────────────────────────────
            m = _RE_SECTION.search(upper)
            if m and current_division == 'DATA':
                current_section = m.group(1).upper()
                current_fd = None   # reset FD ao mudar de seção
                var_buffer = ''
                i += 1
                continue

            # ── Seção na PROCEDURE DIVISION ───────────────────────────────
            m = _RE_SECTION_DEF.match(upper)
            if m and current_division == 'PROCEDURE':
                current_section_name = m.group(1).upper()
                i += 1
                continue

            # ══════════════════════════════════════════════════════════════
            #  IDENTIFICATION DIVISION
            # ══════════════════════════════════════════════════════════════
            if current_division == 'IDENTIFICATION':
                self._parse_identification(content, program)

            # ══════════════════════════════════════════════════════════════
            #  DATA DIVISION
            # ══════════════════════════════════════════════════════════════
            elif current_division == 'DATA':
                # FD — só válido dentro da FILE SECTION
                m = _RE_FD.match(upper)
                if m and current_section == 'FILE':
                    current_fd = m.group(1).upper()
                    var_buffer = ''
                    i += 1
                    continue

                # Variável — pode ocupar múltiplas linhas
                m = _RE_VARIABLE.match(upper)
                if m:
                    # Flush variável anterior se houver
                    if var_buffer:
                        self._flush_variable(
                            var_buffer, var_start_line,
                            current_section, current_fd, program
                        )
                    var_buffer = content
                    var_start_line = line.line_number
                elif var_buffer and not upper.startswith(('*', '/')):
                    # Continuação da declaração da variável
                    var_buffer += ' ' + content

                # Checa se termina com ponto
                if var_buffer and var_buffer.rstrip().endswith('.'):
                    self._flush_variable(
                        var_buffer, var_start_line,
                        current_section, current_fd, program
                    )
                    var_buffer = ''

            # ══════════════════════════════════════════════════════════════
            #  PROCEDURE DIVISION
            # ══════════════════════════════════════════════════════════════
            elif current_division == 'PROCEDURE':

                # Parágrafo
                m = _RE_PARA.match(upper)
                if m and '.' in content:
                    name = m.group(1).upper()
                    # Ignora palavras reservadas que podem aparecer aqui
                    if not self._is_reserved_word(name):
                        # Salva comentários acumulados para esse parágrafo
                        if comments_buffer:
                            para_comments[name] = list(comments_buffer)
                        comments_buffer.clear()

                        current_para = Paragraph(
                            name=name,
                            section=current_section_name,
                            source='',
                            line_start=line.line_number,
                            line_end=line.line_number,
                            comments=para_comments.get(name, [])
                        )
                        program.paragraphs.append(current_para)
                        i += 1
                        continue

                if current_para:
                    current_para.source += content + '\n'
                    current_para.line_end = line.line_number

                    # PERFORM
                    for m in _RE_PERFORM.finditer(upper):
                        target = m.group(1).upper()
                        if target not in ('UNTIL', 'VARYING', 'WITH', 'TIMES', 'THROUGH', 'THRU'):
                            if target not in current_para.performs:
                                current_para.performs.append(target)

                    # CALL
                    m = _RE_CALL.search(upper)
                    if m:
                        called = m.group(1).upper()
                        params = []
                        mu = _RE_USING.search(upper)
                        if mu:
                            params = [
                                p.strip().rstrip('.')
                                for p in mu.group(1).split()
                                if p.strip() and not p.strip().upper() in
                                   ('BY', 'REFERENCE', 'CONTENT', 'VALUE')
                            ]
                        ext_call = ExternalCall(
                            program_name=called,
                            paragraph=current_para.name,
                            parameters=params,
                            line=line.line_number,
                            called_by=[]
                        )
                        current_para.external_calls.append(ext_call)

                    # COMPUTE
                    m = _RE_COMPUTE.match(upper)
                    if m:
                        compute = ComputeBlock(
                            paragraph=current_para.name,
                            target_variable=m.group(1).upper(),
                            formula=m.group(2).rstrip('.').strip(),
                            line=line.line_number
                        )
                        current_para.compute_blocks.append(compute)

                    # IF / EVALUATE — captura multi-linha
                    if upper.startswith('IF ') or upper.startswith('EVALUATE '):
                        block_type = 'IF' if upper.startswith('IF') else 'EVALUATE'
                        block_buffer = [content]
                        block_start = line.line_number
                    elif block_type:
                        block_buffer.append(content)
                        # Termina no END-IF / END-EVALUATE ou ponto
                        end_token = f'END-{block_type}'
                        if end_token in upper or (upper.endswith('.') and len(block_buffer) > 1):
                            source_block = '\n'.join(block_buffer)
                            refs = self._extract_variable_refs(source_block, program)
                            db = DecisionBlock(
                                type=DecisionType[block_type],
                                paragraph=current_para.name,
                                source=source_block,
                                line_start=block_start,
                                line_end=line.line_number,
                                variables_referenced=refs
                            )
                            current_para.decision_blocks.append(db)
                            block_buffer = []
                            block_type = None

            i += 1

        # Flush variável pendente
        if var_buffer:
            self._flush_variable(var_buffer, var_start_line, current_section, None, program)

        return program

    # ──────────────────────────────────────────────────────────────────────────
    #  Helpers
    # ──────────────────────────────────────────────────────────────────────────

    def _parse_identification(self, content: str, program: CobolProgram):
        m = _RE_PROGRAM_ID.search(content)
        if m:
            program.metadata.program_id = m.group(1).upper()
        m = _RE_AUTHOR.search(content)
        if m:
            program.metadata.author = m.group(1).strip().rstrip('.')
        m = _RE_DATE.search(content)
        if m:
            program.metadata.date_written = m.group(1).strip().rstrip('.')
        m = _RE_REMARKS.search(content)
        if m:
            program.metadata.remarks = m.group(1).strip().rstrip('.')

    def _flush_variable(
        self,
        raw: str,
        line_number: int,
        section: str | None,
        fd_name: str | None,
        program: CobolProgram
    ):
        raw = raw.strip().rstrip('.')
        m = _RE_VARIABLE.match(raw.upper())
        if not m:
            return

        level = int(m.group(1))
        name = m.group(2).upper()

        if name in ('FILLER', 'COPY'):
            return

        # PIC
        pm = _RE_PIC.search(raw)
        pic = (pm.group(1) or pm.group(2)).upper() if pm else None

        # VALUE
        vm = _RE_VALUE.search(raw)
        value = vm.group(1).strip().strip("'\"") if vm else None

        # Seção
        sec_map = {
            'FILE': SectionType.FILE,
            'WORKING-STORAGE': SectionType.WORKING_STORAGE,
            'LOCAL-STORAGE': SectionType.LOCAL_STORAGE,
            'LINKAGE': SectionType.LINKAGE,
        }
        sec = sec_map.get(section or '', SectionType.WORKING_STORAGE)

        var = Variable(
            level=level,
            name=name,
            picture=pic,
            value=value,
            section=sec,
            line=line_number,
            is_input_param=(sec == SectionType.LINKAGE),
            is_output_param=(sec == SectionType.LINKAGE),
        )

        # Cláusula 88
        if level == 88:
            values = [value] if value else []
            # Tenta pegar o pai (último var adicionada)
            parent_list = (
                program.working_storage
                if sec in (SectionType.WORKING_STORAGE, SectionType.LOCAL_STORAGE)
                else program.linkage_section
            )
            parent_name = parent_list[-1].name if parent_list else 'UNKNOWN'
            cond = Condition88(
                name=name,
                values=values,
                parent_variable=parent_name,
                line=line_number
            )
            if parent_list:
                parent_list[-1].conditions_88.append(cond)
            return

        if fd_name:
            # Pertence a um layout de arquivo
            existing_fd = next((f for f in program.file_layouts if f.name == fd_name), None)
            if not existing_fd:
                existing_fd = FileLayout(
                    name=fd_name,
                    record_name='',
                    fields=[],
                    line=line_number
                )
                program.file_layouts.append(existing_fd)
            if level == 1:
                existing_fd.record_name = name
            existing_fd.fields.append(var)
        elif sec == SectionType.LINKAGE:
            program.linkage_section.append(var)
        else:
            program.working_storage.append(var)

    def _extract_variable_refs(
        self, source: str, program: CobolProgram
    ) -> dict[str, str]:
        refs: dict[str, str] = {}
        all_var_names = {
            v.name for v in program.working_storage + program.linkage_section
        }
        for m in _RE_VARREF.finditer(source.upper()):
            name = m.group(1)
            if name in all_var_names:
                var = program.get_variable(name)
                refs[name] = var.picture or 'GROUP' if var else 'UNKNOWN'
        return refs

    _RESERVED = {
        'STOP', 'RUN', 'EXIT', 'GOBACK', 'PERFORM', 'MOVE', 'IF', 'ELSE',
        'END', 'COMPUTE', 'ADD', 'SUBTRACT', 'MULTIPLY', 'DIVIDE', 'EVALUATE',
        'WHEN', 'READ', 'WRITE', 'OPEN', 'CLOSE', 'CALL', 'USING', 'GIVING',
        'INITIALIZE', 'INSPECT', 'STRING', 'UNSTRING', 'ACCEPT', 'DISPLAY',
        'END-IF', 'END-EVALUATE', 'END-READ', 'END-WRITE', 'END-PERFORM',
        'END-CALL', 'END-STRING', 'END-UNSTRING', 'END-COMPUTE',
        'NOT', 'AND', 'OR', 'TRUE', 'FALSE', 'OTHER', 'CONTINUE',
        'PROCEDURE', 'DIVISION', 'SECTION',
    }

    def _is_reserved_word(self, word: str) -> bool:
        if word in self._RESERVED:
            return True
        # Tokens como END-xxx são reservados
        if word.startswith('END-'):
            return True
        return False
