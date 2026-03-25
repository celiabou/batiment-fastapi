from __future__ import annotations

from itertools import islice

from intelligence.config import Config
from intelligence.idf import IDF_DEPARTMENTS, get_department, get_department_by_city
from intelligence.models import QueryTask, SearchScope


class QueryBuilder:
    def __init__(self, config: Config):
        self.config = config

    def build_scopes(
        self,
        department_codes: list[str] | None = None,
        cities: list[str] | None = None,
    ) -> list[SearchScope]:
        scopes: list[SearchScope] = []
        scope_keys: set[tuple[str, str | None]] = set()

        def _append_scope(scope: SearchScope) -> None:
            key = (scope.department_code, scope.city)
            if key in scope_keys:
                return
            scope_keys.add(key)
            scopes.append(scope)

        if cities:
            for city in cities:
                department = get_department_by_city(city)
                if not department:
                    continue
                _append_scope(
                    SearchScope(
                        department_code=department.code,
                        department_name=department.name,
                        city=city,
                    )
                )

        if department_codes:
            for code in department_codes:
                department = get_department(code)
                if not department:
                    continue
                _append_scope(
                    SearchScope(department_code=department.code, department_name=department.name)
                )

                per_department = int(self.config.geo.get("cities_per_department", 3))
                for city in islice(department.cities, per_department):
                    _append_scope(
                        SearchScope(
                            department_code=department.code,
                            department_name=department.name,
                            city=city,
                        )
                    )

        if scopes:
            return scopes

        default_codes = self.config.geo.get("default_department_codes", [])
        for code in default_codes:
            department = get_department(code)
            if not department:
                continue
            _append_scope(
                SearchScope(department_code=department.code, department_name=department.name)
            )

            per_department = int(self.config.geo.get("cities_per_department", 3))
            for city in islice(department.cities, per_department):
                _append_scope(
                    SearchScope(
                        department_code=department.code,
                        department_name=department.name,
                        city=city,
                    )
                )

        if scopes:
            return scopes

        # Last fallback: entire IDF.
        for department in IDF_DEPARTMENTS:
            _append_scope(
                SearchScope(department_code=department.code, department_name=department.name)
            )

        return scopes

    def build_queries(
        self,
        scopes: list[SearchScope],
        max_queries: int | None = None,
    ) -> list[QueryTask]:
        lots = [x.strip() for x in self.config.querying.get("lots", []) if x.strip()]
        intents = [x.strip() for x in self.config.querying.get("intents", []) if x.strip()]
        platform_domains = [x.strip() for x in self.config.querying.get("platform_domains", []) if x.strip()]
        social_domains = [x.strip() for x in self.config.querying.get("social_domains", []) if x.strip()]
        max_per_scope = int(self.config.querying.get("max_queries_per_scope", 24))

        tasks: list[QueryTask] = []

        for scope in scopes:
            generated = 0
            location_tokens = [scope.city] if scope.city else [scope.department_name, scope.department_code]
            location_clause = " OR ".join(f'"{token}"' for token in location_tokens if token)
            if not location_clause:
                location_clause = '"Ile-de-France"'

            lot_intent_pairs = [(lot, intent) for intent in intents for lot in lots]

            def _base_query(lot: str, intent: str) -> str:
                return (
                    f'("{lot}") AND ("{intent}") AND ({location_clause}) '
                    f'AND ("Ile-de-France" OR "IDF")'
                )

            def _push(query: str, channel: str, lot: str, intent: str) -> bool:
                nonlocal generated
                if generated >= max_per_scope:
                    return False
                tasks.append(
                    QueryTask(
                        query=query,
                        scope=scope,
                        channel=channel,
                        lot=lot,
                        intent=intent,
                    )
                )
                generated += 1
                return True

            # Pass 1: maximize lot diversity before domain-specific queries.
            for lot, intent in lot_intent_pairs:
                if not _push(_base_query(lot, intent), "google", lot, intent):
                    break

            # Pass 2: enrich with platform-domain queries.
            if generated < max_per_scope:
                for domain in platform_domains[:2]:
                    for lot, intent in lot_intent_pairs:
                        if not _push(f"{_base_query(lot, intent)} site:{domain}", "platform", lot, intent):
                            break
                    if generated >= max_per_scope:
                        break

            # Pass 3: enrich with social-domain queries.
            if generated < max_per_scope:
                for domain in social_domains[:2]:
                    for lot, intent in lot_intent_pairs:
                        if not _push(f"{_base_query(lot, intent)} site:{domain}", "social", lot, intent):
                            break
                    if generated >= max_per_scope:
                        break

        if max_queries is not None:
            return tasks[: max(0, max_queries)]
        return tasks
