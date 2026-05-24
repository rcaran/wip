# ADVANCED DATA FLOW + LLM SEMANTIC INFERENCE

import re
from collections import defaultdict

# ======================================================================
# DATA FLOW (já avançado)
# ======================================================================

class DataFlowAnalyzer:
    def __init__(self):
        self.flow_graph = defaultdict(set)

    def register_move(self, source: str, target: str):
        if source and target:
            self.flow_graph[target.upper()].add(source.upper())

    def register_compute(self, target: str, expression: str):
        vars_in_expr = re.findall(r'[A-Z0-9\-]+', expression.upper())
        for var in vars_in_expr:
            if not var.isdigit():
                self.flow_graph[target.upper()].add(var)

    def resolve_origins(self, var: str):
        var = var.upper()
        visited = set()
        result = set()

        def dfs(v):
            if v in visited:
                return
            visited.add(v)

            if v not in self.flow_graph or not self.flow_graph[v]:
                result.add(v)
                return

            for parent in self.flow_graph[v]:
                dfs(parent)

        dfs(var)
        return result

# ======================================================================
# LLM SEMANTIC INFERENCE LAYER
# ======================================================================

class SemanticInferenceEngine:
    def __init__(self, llm_client):
        self.llm = llm_client

    def infer_variable_meaning(self, var_name: str, origins: list, context: str):
        prompt = f"""
Você é um especialista em sistemas COBOL e análise de regras de negócio.

Variável: {var_name}
Origem (data flow): {origins}
Contexto do código: {context}

Responda em JSON:
{{
  "business_meaning": "",
  "rule_type": "validation|calculation|flag|state|unknown",
  "confidence": 0.0
}}
"""
        return self.llm(prompt)

    def infer_node_semantics(self, node):
        context = node.statement
        results = {}

        for var, origins in node.meta.get("data_flow_origins", {}).items():
            results[var] = self.infer_variable_meaning(var, origins, context)

        return results

# ======================================================================
# INTEGRAÇÃO NO BUILDER
# ======================================================================

class ProcedureDivisionBuilder:
    def __init__(self, data_division, dialect=None, llm_client=None):
        self.data_flow = DataFlowAnalyzer()
        self.semantic_engine = SemanticInferenceEngine(llm_client) if llm_client else None

    def _extract_variable_names(self, statement: str):
        found = []
        words = re.findall(r'[A-Z0-9\-]+', statement.upper())

        for w in words:
            origins = self.data_flow.resolve_origins(w)

            for origin in origins:
                if origin in self._var_map:
                    found.append(self._var_map[origin])

        return found

    def add_arithmetic(self, kind: str, text: str, line: int):
        move_match = re.match(r'(?i)MOVE\s+([A-Z0-9\-]+)\s+TO\s+([A-Z0-9\-]+)', text)
        compute_match = re.match(r'(?i)COMPUTE\s+([A-Z0-9\-]+)\s*=\s+(.+)', text)

        if move_match:
            self.data_flow.register_move(move_match.group(1), move_match.group(2))

        elif compute_match:
            self.data_flow.register_compute(compute_match.group(1), compute_match.group(2))

        n = self._add_node(kind, text, line, {"type": "TRANSFORMATION"})
        self._enrich_with_semantics(n)
        return n.id

    def add_generic(self, kind: str, text: str, line: int, meta: dict = None):
        meta = meta or {}

        move_match = re.match(r'(?i)MOVE\s+([A-Z0-9\-]+)\s+TO\s+([A-Z0-9\-]+)', text)
        if move_match:
            self.data_flow.register_move(move_match.group(1), move_match.group(2))

        compute_match = re.match(r'(?i)COMPUTE\s+([A-Z0-9\-]+)\s*=\s+(.+)', text)
        if compute_match:
            self.data_flow.register_compute(compute_match.group(1), compute_match.group(2))

        n = self._add_node(kind, text, line, meta)
        self._enrich_with_semantics(n)
        return n.id

    # --------------------------------------------------
    # ENRIQUECIMENTO COM LLM
    # --------------------------------------------------
    def _enrich_with_semantics(self, node):
        referenced = self._extract_variable_names(node.statement)

        resolved_map = {}
        for v in referenced:
            if v:
                origins = self.data_flow.resolve_origins(v.name)
                resolved_map[v.name] = list(origins)

        node.meta["referenced_vars"] = list(resolved_map.keys())
        node.meta["data_flow_origins"] = resolved_map

        if self.semantic_engine:
            node.meta["semantic_inference"] = self.semantic_engine.infer_node_semantics(node)

# ======================================================================
# RESULTADO FINAL
# ======================================================================

# Cada node agora contém:
# ✔ data flow completo
# ✔ múltiplas origens
# ✔ inferência semântica automática via LLM
#
# Exemplo:
# "semantic_inference": {
#   "WS-STATUS": {
#       "business_meaning": "status do cliente",
#       "rule_type": "state",
#       "confidence": 0.92
#   }
# }

# ======================================================================
# RULE CONSOLIDATION ENGINE
# ======================================================================

class RuleConsolidationEngine:
    def __init__(self, llm_client=None):
        self.llm = llm_client

    # --------------------------------------------------
    # EXTRAÇÃO DE REGRAS DOS NODES
    # --------------------------------------------------
    def extract_rules(self, graph_nodes):
        rules = []

        for node in graph_nodes:
            if "semantic_inference" not in node["meta"]:
                continue

            for var, sem in node["meta"]["semantic_inference"].items():
                rule = {
                    "variable": var,
                    "statement": node["statement"],
                    "type": sem.get("rule_type"),
                    "meaning": sem.get("business_meaning"),
                    "confidence": sem.get("confidence", 0.5)
                }
                rules.append(rule)

        return rules

    # --------------------------------------------------
    # DEDUPLICAÇÃO
    # --------------------------------------------------
    def deduplicate_rules(self, rules):
        unique = {}

        for r in rules:
            key = (r["variable"], r["meaning"], r["type"])

            if key not in unique or r["confidence"] > unique[key]["confidence"]:
                unique[key] = r

        return list(unique.values())

    # --------------------------------------------------
    # AGRUPAMENTO POR DOMÍNIO
    # --------------------------------------------------
    def group_by_domain(self, rules):
        grouped = {}

        for r in rules:
            domain = r["variable"].split("-")[0]

            if domain not in grouped:
                grouped[domain] = []

            grouped[domain].append(r)

        return grouped

    # --------------------------------------------------
    # CONSOLIDAÇÃO COM LLM
    # --------------------------------------------------
    def consolidate_with_llm(self, grouped_rules):
        if not self.llm:
            return grouped_rules

        prompt = f"""
Você é um analista de negócios.

Abaixo estão regras extraídas de um sistema COBOL agrupadas por domínio.

Tarefas:
1. Remover redundâncias
2. Consolidar regras similares
3. Melhorar descrição em linguagem de negócio
4. Organizar em seções

Entrada:
{grouped_rules}

Saída (JSON estruturado):
"""

        return self.llm(prompt)

    # --------------------------------------------------
    # PIPELINE COMPLETO
    # --------------------------------------------------
    def run(self, graph_nodes):
        rules = self.extract_rules(graph_nodes)
        rules = self.deduplicate_rules(rules)
        grouped = self.group_by_domain(rules)
        consolidated = self.consolidate_with_llm(grouped)

        return consolidated

# ======================================================================
# RESULTADO FINAL
# ======================================================================

# Agora você tem:
# ✔ Extração automática de regras
# ✔ Deduplicação
# ✔ Agrupamento por domínio
# ✔ Consolidação com LLM
#
# Output final pronto para documentação de negócio

