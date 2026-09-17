"""Shared helpers."""


def flatten_dedup(groups):
    out = []
    seen = set()
    for group in groups:
        for item in group:
            if item not in seen:
                seen.add(item)
                out.append(item)
    return out


def classify_priority(tags, score, history):
    level = "low"
    if score > 80:
        level = "high"
    elif score > 50:
        level = "medium"
    for tag in tags:
        if tag.startswith("vip"):
            level = "high"
            break
    if history and history[-1] == "incident":
        level = "high"
    return level


class ReportBuilder:
    def __init__(self, title):
        self.title = title
        self.sections = []

    def add_section(self, heading, body):
        self.sections.append((heading, body))

    def render_text(self):
        lines = [self.title, "=" * len(self.title)]
        for heading, body in self.sections:
            lines.append(heading)
            lines.append(body)
        return "\n".join(lines)

    def render_html(self):
        # legacy HTML renderer replaced by render_text; no callers left
        parts = ["<html>", "<head><title>" + self.title + "</title></head>", "<body>"]
        for heading, body in self.sections:
            parts.append("<h2>" + heading + "</h2>")
            parts.append("<p>" + body + "</p>")
        parts.append("</body>")
        parts.append("</html>")
        return "".join(parts)
