"""MCP tool registry cho custom server.

Hiện tại tập trung vào tool tìm nhạc Việt.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode
from urllib.request import urlopen

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class MCPToolResult:
    """Kết quả chuẩn hóa khi gọi MCP tool."""

    ok: bool
    content: list[dict[str, Any]]


class MCPToolRegistry:
    """Registry đơn giản cho MCP tools nội bộ server."""

    def list_tools(self) -> list[dict[str, Any]]:
        """Trả danh sách tool theo format gần JSON-Schema."""
        return [
            {
                "name": "search_vietnamese_music",
                "description": "Tìm nhạc Việt theo từ khóa (artist/bài hát), trả metadata và link nghe.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "song_name": {
                            "type": "string",
                            "description": "Tên bài hát cần tìm, ví dụ: Nơi này có anh",
                        },
                        "query": {
                            "type": "string",
                            "description": "Từ khóa tìm kiếm, ví dụ: Son Tung M-TP",
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Số kết quả tối đa (1-20)",
                            "minimum": 1,
                            "maximum": 20,
                            "default": 5,
                        },
                    },
                    "required": [],
                },
            }
        ]

    async def call_tool(self, name: str, arguments: dict[str, Any] | None) -> MCPToolResult:
        """Gọi 1 tool theo tên."""
        arguments = arguments or {}

        if name == "search_vietnamese_music":
            return self._tool_search_vietnamese_music(arguments)

        return MCPToolResult(
            ok=False,
            content=[{"type": "text", "text": f"Tool không tồn tại: {name}"}],
        )

    def _tool_search_vietnamese_music(self, arguments: dict[str, Any]) -> MCPToolResult:
        song_name = str(arguments.get("song_name", "")).strip()
        query = song_name or str(arguments.get("query", "")).strip()
        if not query:
            return MCPToolResult(
                ok=False,
                content=[{"type": "text", "text": "Thiếu tham số song_name hoặc query"}],
            )

        raw_limit = arguments.get("limit", 5)
        try:
            limit = max(1, min(int(raw_limit), 20))
        except (TypeError, ValueError):
            limit = 5

        try:
            params = urlencode({"q": query, "limit": str(limit)})
            url = f"https://api.deezer.com/search?{params}"

            with urlopen(url, timeout=12) as resp:
                data = json.loads(resp.read().decode("utf-8"))

            items = data.get("data", [])
            tracks = []
            for item in items[:limit]:
                tracks.append(
                    {
                        "title": item.get("title"),
                        "artist": (item.get("artist") or {}).get("name"),
                        "album": (item.get("album") or {}).get("title"),
                        "deezer_url": item.get("link"),
                        "preview_url": item.get("preview"),
                        "duration": item.get("duration"),
                    }
                )

            text = f"Tìm thấy {len(tracks)} kết quả nhạc cho: {query}"
            return MCPToolResult(
                ok=True,
                content=[
                    {"type": "text", "text": text},
                    {
                        "type": "json",
                        "json": {
                            "request_body": {
                                "song_name": song_name,
                                "query": query,
                                "limit": limit,
                            },
                            "tracks": tracks,
                        },
                    },
                ],
            )
        except Exception as e:
            logger.error("MCP tool search_vietnamese_music failed: %s", e, exc_info=True)
            return MCPToolResult(
                ok=False,
                content=[{"type": "text", "text": f"Lỗi gọi Deezer API: {e}"}],
            )
