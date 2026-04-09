"""Tests for promptise.server dependency injection."""

from __future__ import annotations

from promptise.mcp.server._decorators import _DependsMarker
from promptise.mcp.server._di import DependencyResolver, Depends

# =====================================================================
# Depends marker
# =====================================================================


class TestDependsMarker:
    def test_creates_marker(self):
        def my_dep():
            return 42

        marker = Depends(my_dep)
        assert isinstance(marker, _DependsMarker)
        assert marker.dependency is my_dep
        assert marker.use_cache is True

    def test_creates_marker_no_cache(self):
        def my_dep():
            return 42

        marker = Depends(my_dep, use_cache=False)
        assert marker.use_cache is False


# =====================================================================
# DependencyResolver
# =====================================================================


class TestDependencyResolver:
    async def test_resolve_sync_callable(self):
        def get_config():
            return {"key": "value"}

        async def handler(query: str, config: dict = Depends(get_config)) -> dict:
            return {"query": query, "config": config}

        resolver = DependencyResolver()
        resolved = await resolver.resolve(handler, {"query": "test"})
        assert resolved["query"] == "test"
        assert resolved["config"] == {"key": "value"}
        await resolver.cleanup()

    async def test_resolve_async_callable(self):
        async def get_db():
            return "db_connection"

        async def handler(sql: str, db: str = Depends(get_db)) -> str:
            return f"{db}:{sql}"

        resolver = DependencyResolver()
        resolved = await resolver.resolve(handler, {"sql": "SELECT 1"})
        assert resolved["db"] == "db_connection"
        await resolver.cleanup()

    async def test_resolve_sync_generator(self):
        cleanup_called = False

        def get_resource():
            nonlocal cleanup_called
            yield "resource_value"
            cleanup_called = True

        async def handler(x: int, res: str = Depends(get_resource)) -> str:
            return res

        resolver = DependencyResolver()
        resolved = await resolver.resolve(handler, {"x": 1})
        assert resolved["res"] == "resource_value"
        assert not cleanup_called

        await resolver.cleanup()
        assert cleanup_called

    async def test_resolve_async_generator(self):
        cleanup_called = False

        async def get_connection():
            nonlocal cleanup_called
            yield "async_connection"
            cleanup_called = True

        async def handler(x: int, conn: str = Depends(get_connection)) -> str:
            return conn

        resolver = DependencyResolver()
        resolved = await resolver.resolve(handler, {"x": 1})
        assert resolved["conn"] == "async_connection"
        assert not cleanup_called

        await resolver.cleanup()
        assert cleanup_called

    async def test_caching_same_dependency(self):
        call_count = 0

        def get_service():
            nonlocal call_count
            call_count += 1
            return f"service_{call_count}"

        async def handler(
            a: str = Depends(get_service),
            b: str = Depends(get_service),
        ) -> tuple:
            return (a, b)

        resolver = DependencyResolver()
        resolved = await resolver.resolve(handler, {})
        # With caching, same dependency should only be called once
        assert resolved["a"] == "service_1"
        assert resolved["b"] == "service_1"
        assert call_count == 1
        await resolver.cleanup()

    async def test_no_cache_calls_multiple_times(self):
        call_count = 0

        def get_service():
            nonlocal call_count
            call_count += 1
            return f"service_{call_count}"

        async def handler(
            a: str = Depends(get_service, use_cache=False),
            b: str = Depends(get_service, use_cache=False),
        ) -> tuple:
            return (a, b)

        resolver = DependencyResolver()
        resolved = await resolver.resolve(handler, {})
        assert resolved["a"] == "service_1"
        assert resolved["b"] == "service_2"
        assert call_count == 2
        await resolver.cleanup()

    async def test_non_dep_params_unchanged(self):
        def get_dep():
            return "dep_value"

        async def handler(query: str, dep: str = Depends(get_dep)) -> str:
            return ""

        resolver = DependencyResolver()
        resolved = await resolver.resolve(handler, {"query": "hello"})
        assert resolved["query"] == "hello"
        assert resolved["dep"] == "dep_value"
        await resolver.cleanup()

    async def test_cleanup_runs_in_reverse_order(self):
        order: list[str] = []

        def dep_a():
            yield "a"
            order.append("cleanup_a")

        def dep_b():
            yield "b"
            order.append("cleanup_b")

        async def handler(
            a: str = Depends(dep_a),
            b: str = Depends(dep_b),
        ) -> str:
            return ""

        resolver = DependencyResolver()
        await resolver.resolve(handler, {})
        await resolver.cleanup()
        assert order == ["cleanup_b", "cleanup_a"]

    async def test_cleanup_swallows_errors(self):
        def bad_dep():
            yield "value"
            raise RuntimeError("cleanup error")

        async def handler(x: str = Depends(bad_dep)) -> str:
            return x

        resolver = DependencyResolver()
        resolved = await resolver.resolve(handler, {})
        assert resolved["x"] == "value"
        # Should not raise
        await resolver.cleanup()
