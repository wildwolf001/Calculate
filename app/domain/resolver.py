"""Word -> grounded value. Exact match, alias, prefix, fuzzy, then clarify.

The resolver never guesses silently: a term that maps to zero or multiple
values becomes an ambiguity event, and the controller asks the user.
"""
from __future__ import annotations

import json
from difflib import SequenceMatcher

from app.config.settings import DIM_VALUES_PATH
from app.core.interfaces import GroundResult
from app.domain.semantic.loader import SemanticLayer

DIM_VALUE_KEY = {
    "category": "categories.category_name_zh",
    "seller": "sellers.seller_name",
    "product": "products.product_name",
    "state": "customers.customer_state",
    "city": "customers.customer_city",
    "seller_state": "sellers.seller_state",
    "payment_type": "order_payments.payment_type",
    "order_status": "orders.order_status",
}

ALIAS_KEY = {
    "category": "categories.category_name_zh",
    "payment_type": "order_payments.payment_type",
    "order_status": "orders.order_status",
}

FUZZY_THRESHOLD = 0.72
FAMILY_DIMS = {"product"}
FAMILY_MAX = 50


class RuleGrounder:
    def __init__(self, sem: SemanticLayer, dim_values_path=DIM_VALUES_PATH):
        self.sem = sem
        self.data = json.loads(dim_values_path.read_text(encoding="utf-8"))
        self.values = self.data.get("values", {})
        self.aliases = self.data.get("aliases", {})
        self.id_maps = self.data.get("id_maps", {})
        self.id_reverse = {
            dim: {str(v): name for name, v in mapping.items()}
            for dim, mapping in self.id_maps.items()
        }

    # ---------- helpers ----------
    def _candidates(self, dim: str) -> list[str]:
        key = DIM_VALUE_KEY.get(dim)
        return list(self.values.get(key, [])) if key else []

    def _alias_reverse(self, dim: str) -> dict[str, str]:
        key = ALIAS_KEY.get(dim)
        if not key:
            return {}
        out: dict[str, str] = {}
        for canonical, words in (self.aliases.get(key) or {}).items():
            for w in words:
                out[w] = canonical
        return out

    def _to_ids(self, dim: str, canonical: list[str]) -> list:
        mapping = self.id_maps.get(dim, {})
        return [mapping.get(v, v) for v in canonical]

    def _match_one(self, dim: str, term: str) -> tuple[list[str], list[str], list[str]]:
        """Return (exact_or_alias, prefix, fuzzy) candidates for one term."""
        term = str(term).strip()
        cands = self._candidates(dim)
        aliases = self._alias_reverse(dim)
        if term in cands:
            return [term], [], []
        if term in aliases:
            return [aliases[term]], [], []
        prefix = [c for c in cands if c.startswith(term) or term in c]
        if prefix:
            return [], prefix, []
        scored = sorted(
            ((SequenceMatcher(None, term, c).ratio(), c) for c in cands),
            reverse=True,
        )
        fuzzy = [c for score, c in scored if score >= FUZZY_THRESHOLD]
        if len(fuzzy) > 1:
            top = scored[0][0]
            fuzzy = [c for score, c in scored if top - score < 0.05]
        return [], [], fuzzy

    # ---------- main ----------
    def ground(self, slots: dict, unresolved: list[str], context: dict) -> GroundResult:
        bindings: list[dict] = []
        notes: list[str] = []
        filters = slots.get("filters") or {}

        for dim, spec in filters.items():
            if dim not in DIM_VALUE_KEY:
                continue
            value = spec.get("value") if isinstance(spec, dict) else spec
            op = spec.get("op", "eq") if isinstance(spec, dict) else "eq"
            terms = value if isinstance(value, list) else [value]
            canonical: list[str] = []
            family = False
            for term in terms:
                if term is None:
                    continue
                id_name = self.id_reverse.get(dim, {}).get(str(term))
                if id_name:
                    canonical.append(id_name)
                    continue
                exact, prefix, fuzzy = self._match_one(dim, term)
                if exact:
                    pool = exact
                elif prefix:
                    # A family keyword such as 键盘 may cover many SKUs; bind the
                    # whole set instead of forcing the user to pick 20 names.
                    if dim in FAMILY_DIMS and 1 < len(prefix) <= FAMILY_MAX:
                        canonical.extend(prefix)
                        family = True
                        notes.append(
                            f"「{term}」按{self._dim_name(dim)}关键词匹配到 {len(prefix)} 个取值，已按集合查询。"
                        )
                        continue
                    pool = prefix
                else:
                    pool = fuzzy
                if len(pool) == 1:
                    canonical.append(pool[0])
                elif len(pool) > 1:
                    return GroundResult(ambiguity={
                        "field": dim,
                        "term": term,
                        "candidates": pool[:20],
                        "message": f"「{term}」在「{self._dim_name(dim)}」中匹配到多个值，请选择。",
                        "kind": "value_ambiguous",
                    })
                else:
                    return GroundResult(ambiguity={
                        "field": dim,
                        "term": term,
                        "candidates": self._closest(dim, term),
                        "message": f"数据里找不到「{term}」对应的{self._dim_name(dim)}。",
                        "kind": "value_not_found",
                    })
            canonical = list(dict.fromkeys(canonical))
            if canonical:
                binding = {
                    "dim": dim,
                    "term": terms,
                    "values": canonical,
                    "ids": self._to_ids(dim, canonical),
                    "op": "in" if (op == "in" or len(canonical) > 1) else "eq",
                }
                if family:
                    binding["match"] = "family"
                bindings.append(binding)

        if unresolved:
            return GroundResult(
                bindings=bindings,
                ambiguity={
                    "field": "unknown",
                    "term": unresolved[0],
                    "candidates": [],
                    "message": f"没有识别出「{', '.join(unresolved)}」对应的指标或维度。",
                    "kind": "unresolved",
                },
            )

        return GroundResult(bindings=bindings, notes=notes)

    def _closest(self, dim: str, term: str, n: int = 5) -> list[str]:
        scored = sorted(
            ((SequenceMatcher(None, str(term), c).ratio(), c) for c in self._candidates(dim)),
            reverse=True,
        )
        return [c for _, c in scored[:n]]

    def _dim_name(self, dim: str) -> str:
        d = self.sem.dimensions.get(dim)
        return d.raw.get("name_zh", dim) if d else dim
