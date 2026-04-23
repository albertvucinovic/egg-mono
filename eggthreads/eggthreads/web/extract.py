from __future__ import annotations


def html_to_markdown(html: str, url: str | None = None) -> str:
    """Extract readable markdown from raw HTML.

    Uses trafilatura when available, falls back to a minimal text
    reduction so the tool degrades gracefully on machines that haven't
    installed the optional dep.
    """
    if not html:
        return ""
    try:
        import trafilatura  # type: ignore
    except Exception:
        return _strip_tags(html)

    try:
        extracted = trafilatura.extract(
            html,
            url=url,
            output_format="markdown",
            include_links=True,
            include_tables=True,
            favor_recall=True,
        )
    except Exception:
        extracted = None
    if extracted:
        return extracted.strip()
    return _strip_tags(html)


def _strip_tags(html: str) -> str:
    import re
    text = re.sub(r"<script[\s\S]*?</script>", " ", html, flags=re.IGNORECASE)
    text = re.sub(r"<style[\s\S]*?</style>", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text
