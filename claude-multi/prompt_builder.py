"""
PromptBuilder — converte CobolProgram em payload otimizado para o Opus 4.6.

Princípios:
  - Contexto compartilhado (dicionário + grafo) vai em TODO chunk
  - Cada chunk tem parágrafos ativos com todo o seu contexto inline
  - Código morto é sinalizado mas separado
  - Comentários originais são preservados como contexto semântico
"""
from __future__ import annotations
import json
from dataclasses import asdict, dataclass
from extractor.models import CobolProgram, Paragraph, Variable


SYSTEM_PROMPT = """\
Você é um especialista sênior em sistemas legados COBOL e engenharia de software bancária/empresarial.

Sua tarefa é documentar com MÁXIMO DETALHE todas as regras de negócio de um programa COBOL.

## Diretrizes obrigatórias

1. **NENHUMA regra pode ser omitida** — documente cada IF, EVALUATE, COMPUTE e lógica condicional
2. **Nomeie cada regra** com um identificador único: RN-001, RN-002, ...
3. **Para cada regra, forneça:**
   - Identificador (RN-XXX)
   - Parágrafo de origem e fluxo de chamada (quem aciona essa regra)
   - Condição exata (o que dispara a regra)
   - Ação resultante (o que acontece)
   - Variáveis envolvidas com seus tipos e valores possíveis (incluindo cláusulas 88)
   - Dependências externas (se a regra chama outro programa)
   - Observações ou ambiguidades identificadas
4. **Cláusulas 88** devem ser mapeadas ao seu significado de negócio
5. **Fórmulas** (COMPUTE) devem ser documentadas com a expressão matemática em notação legível
6. **Código morto** (parágrafos inalcançáveis) deve ser listado separadamente com aviso
7. **Se houver ambiguidade** de intenção, registre como "⚠ AMBIGUIDADE" e explique
8. **Não invente** informações — se algo não estiver claro no código, diga explicitamente

## Formato de saída

Produza um documento Markdown estruturado com:
- Sumário executivo do programa
- Glossário de variáveis críticas de negócio
- Glossário de condições (cláusulas 88)
- Catálogo de regras de negócio (RN-001, RN-002, ...)
- Mapa de dependências externas
- Código morto identificado (se houver)
"""


@dataclass
class PromptChunk:
    """Um chunk pronto para enviar à API."""
    chunk_index: int
    total_chunks: int
    system: str
    user_message: str
    estimated_tokens: int


class PromptBuilder:

    # Opus 4.6: 1M tokens de contexto, mas usamos margem conservadora
    MAX_CHUNK_TOKENS = 180_000
    # Estimativa grosseira: 1 token ≈ 4 chars
    CHARS_PER_TOKEN = 4

    def build(self, program: CobolProgram) -> list[PromptChunk]:
        """
        Constrói lista de chunks prontos para a API.
        Programas pequenos → 1 chunk. Grandes → múltiplos chunks.
        """
        shared_context = self._build_shared_context(program)
        active_paras = [p for p in program.paragraphs if not p.is_unreachable]
        dead_paras   = [p for p in program.paragraphs if p.is_unreachable]

        # Tenta caber tudo em 1 chunk
        full_payload = self._build_user_message(
            program, shared_context, active_paras, dead_paras,
            chunk_idx=1, total_chunks=1
        )

        if self._estimate_tokens(full_payload) <= self.MAX_CHUNK_TOKENS:
            return [PromptChunk(
                chunk_index=1,
                total_chunks=1,
                system=SYSTEM_PROMPT,
                user_message=full_payload,
                estimated_tokens=self._estimate_tokens(full_payload)
            )]

        # Chunking por parágrafos
        return self._build_chunked(program, shared_context, active_paras, dead_paras)

    # ──────────────────────────────────────────────────────────────────────────
    #  Contexto compartilhado (presente em TODOS os chunks)
    # ──────────────────────────────────────────────────────────────────────────

    def _build_shared_context(self, program: CobolProgram) -> str:
        parts = []

        # Metadados
        meta = program.metadata
        parts.append(f"# PROGRAMA: {meta.program_id}")
        if meta.author:
            parts.append(f"- **Autor:** {meta.author}")
        if meta.date_written:
            parts.append(f"- **Data de escrita:** {meta.date_written}")
        if meta.remarks:
            parts.append(f"- **Observações originais:** {meta.remarks}")
        if program.copybooks_referenced:
            parts.append(f"- **COPYbooks referenciados:** {', '.join(program.copybooks_referenced)}")

        # Grafo de chamadas
        parts.append("\n## GRAFO DE CHAMADAS ENTRE PARÁGRAFOS")
        parts.append("```")
        for para in program.paragraphs:
            status = " [CÓDIGO MORTO]" if para.is_unreachable else ""
            calls = ', '.join(para.performs) if para.performs else '(nenhum)'
            called_by = ', '.join(para.performed_by) if para.performed_by else '(ponto de entrada)'
            parts.append(f"{para.name}{status}")
            parts.append(f"  ← chamado por: {called_by}")
            parts.append(f"  → chama: {calls}")
        parts.append("```")

        # Dicionário de variáveis críticas de negócio
        parts.append("\n## DICIONÁRIO DE VARIÁVEIS DE NEGÓCIO")
        critical_vars = [
            v for v in program.working_storage + program.linkage_section
            if v.picture or v.conditions_88
        ]
        for var in critical_vars:
            param_tag = ""
            if var.is_input_param:
                param_tag = " `[ENTRADA]`"
            elif var.is_output_param:
                param_tag = " `[SAÍDA]`"

            parts.append(
                f"- **{var.name}**{param_tag} | Nível: {var.level:02d} | "
                f"PIC: `{var.picture or 'GROUP'}` | "
                f"Valor padrão: `{var.value or '-'}`"
            )
            if var.read_in:
                parts.append(f"  - Lida em: {', '.join(var.read_in)}")
            if var.written_in:
                parts.append(f"  - Modificada em: {', '.join(var.written_in)}")
            for c88 in var.conditions_88:
                parts.append(
                    f"  - `88 {c88.name}` → valor(es): `{', '.join(c88.values)}`"
                )

        # Layouts de arquivo
        if program.file_layouts:
            parts.append("\n## LAYOUTS DE ARQUIVO (FD)")
            for fd in program.file_layouts:
                parts.append(f"\n### {fd.name} (registro: {fd.record_name})")
                for field in fd.fields:
                    parts.append(
                        f"  - {field.name:30s} | PIC {field.picture or 'GROUP':15s} | "
                        f"Nível {field.level:02d}"
                    )

        return '\n'.join(parts)

    # ──────────────────────────────────────────────────────────────────────────
    #  Payload completo (1 chunk ou parte de um chunk)
    # ──────────────────────────────────────────────────────────────────────────

    def _build_user_message(
        self,
        program: CobolProgram,
        shared_context: str,
        active_paras: list[Paragraph],
        dead_paras: list[Paragraph],
        chunk_idx: int,
        total_chunks: int,
    ) -> str:
        parts = [shared_context]

        if total_chunks > 1:
            parts.append(
                f"\n---\n> **Chunk {chunk_idx}/{total_chunks}** — "
                f"analise os parágrafos abaixo. O contexto compartilhado acima "
                f"está disponível para todos os chunks.\n"
            )

        # Parágrafos ativos com todo o contexto
        parts.append("\n## PARÁGRAFOS ATIVOS (CÓDIGO VIVO)")
        for para in active_paras:
            parts.append(self._format_paragraph(para))

        # Código morto (apenas no último chunk ou chunk único)
        if dead_paras and chunk_idx == total_chunks:
            parts.append("\n## PARÁGRAFOS INALCANÇÁVEIS (CÓDIGO MORTO)")
            parts.append("> ⚠ Os parágrafos abaixo nunca são executados. "
                         "Documente-os com aviso de 'REGRA INATIVA'.\n")
            for para in dead_paras:
                parts.append(self._format_paragraph(para))

        # Instrução final
        parts.append(self._build_instruction(program, chunk_idx, total_chunks))

        return '\n'.join(parts)

    def _format_paragraph(self, para: Paragraph) -> str:
        lines = [f"\n### PARÁGRAFO: `{para.name}`"]

        if para.section:
            lines.append(f"- **Seção:** {para.section}")
        if para.performed_by:
            lines.append(f"- **Chamado por:** {', '.join(para.performed_by)}")
        else:
            lines.append("- **Chamado por:** (ponto de entrada / PERFORM implícito)")
        if para.performs:
            lines.append(f"- **Chama:** {', '.join(para.performs)}")
        if para.is_unreachable:
            lines.append("- ⚠ **CÓDIGO MORTO — parágrafo inalcançável**")
        if para.comments:
            lines.append(f"- **Comentários originais:** {' | '.join(para.comments)}")

        # Blocos de decisão
        if para.decision_blocks:
            lines.append("\n**Blocos de decisão:**")
            for db in para.decision_blocks:
                lines.append(f"\n_Tipo: {db.type.value} | Linhas {db.line_start}–{db.line_end}_")
                if db.variables_referenced:
                    for vname, vpic in db.variables_referenced.items():
                        lines.append(f"  - `{vname}` (PIC {vpic})")
                lines.append("```cobol")
                lines.append(db.source)
                lines.append("```")

        # Computações
        if para.compute_blocks:
            lines.append("\n**Fórmulas (COMPUTE):**")
            for cb in para.compute_blocks:
                lines.append(f"- `{cb.target_variable}` = `{cb.formula}` (linha {cb.line})")

        # Chamadas externas
        if para.external_calls:
            lines.append("\n**Dependências externas (CALL):**")
            for ec in para.external_calls:
                params = ', '.join(ec.parameters) if ec.parameters else 'nenhum'
                lines.append(f"- CALL `{ec.program_name}` USING {params} (linha {ec.line})")

        # Código-fonte completo
        if para.source.strip():
            lines.append("\n**Código-fonte completo:**")
            lines.append("```cobol")
            lines.append(para.source.strip())
            lines.append("```")

        return '\n'.join(lines)

    def _build_instruction(self, program: CobolProgram, chunk_idx: int, total_chunks: int) -> str:
        if total_chunks == 1:
            return (
                f"\n---\n"
                f"Documente **todas** as regras de negócio do programa `{program.metadata.program_id}` "
                f"seguindo rigorosamente o formato e as diretrizes do sistema prompt.\n"
                f"Comece pelo sumário executivo, depois o glossário, depois o catálogo de regras RN-XXX."
            )
        elif chunk_idx < total_chunks:
            return (
                f"\n---\n"
                f"Este é o chunk {chunk_idx}/{total_chunks}. "
                f"Documente as regras dos parágrafos acima (RN-{(chunk_idx-1)*50+1:03d} em diante). "
                f"NÃO repita o sumário executivo — ele será gerado no chunk final."
            )
        else:
            return (
                f"\n---\n"
                f"Este é o chunk final ({chunk_idx}/{total_chunks}). "
                f"Documente as regras dos parágrafos acima, depois produza:\n"
                f"1. O mapa completo de dependências externas\n"
                f"2. O código morto identificado\n"
                f"3. O sumário executivo consolidado do programa `{program.metadata.program_id}`"
            )

    # ──────────────────────────────────────────────────────────────────────────
    #  Chunking
    # ──────────────────────────────────────────────────────────────────────────

    def _build_chunked(
        self,
        program: CobolProgram,
        shared_context: str,
        active_paras: list[Paragraph],
        dead_paras: list[Paragraph],
    ) -> list[PromptChunk]:
        """Divide parágrafos em chunks respeitando o limite de tokens."""
        para_groups: list[list[Paragraph]] = []
        current_group: list[Paragraph] = []
        current_size = self._estimate_tokens(shared_context)

        for para in active_paras:
            para_text = self._format_paragraph(para)
            para_tokens = self._estimate_tokens(para_text)

            if current_size + para_tokens > self.MAX_CHUNK_TOKENS and current_group:
                para_groups.append(current_group)
                current_group = [para]
                current_size = self._estimate_tokens(shared_context) + para_tokens
            else:
                current_group.append(para)
                current_size += para_tokens

        if current_group:
            para_groups.append(current_group)

        total = len(para_groups)
        chunks: list[PromptChunk] = []

        for idx, group in enumerate(para_groups, start=1):
            dead = dead_paras if idx == total else []
            msg = self._build_user_message(
                program, shared_context, group, dead, idx, total
            )
            chunks.append(PromptChunk(
                chunk_index=idx,
                total_chunks=total,
                system=SYSTEM_PROMPT,
                user_message=msg,
                estimated_tokens=self._estimate_tokens(msg)
            ))

        return chunks

    @staticmethod
    def _estimate_tokens(text: str) -> int:
        return len(text) // 4
