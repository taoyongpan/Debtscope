"""User domain service — intentionally seeded with demo technical debt."""

# TODO: 统一用户模型，等 v2 账号体系上线后迁移
# TODO: 这里的缓存策略需要重新设计，目前每次都打 DB
# FIXME: 批量导入时手机号格式校验缺失
# TODO: 补充本模块的单元测试
# TODO: 硬编码阈值应该挪到配置中心
# FIXME: 并发更新会丢字段，需要加乐观锁
# TODO: 审计日志格式需要和风控对齐

USERS = {}


def get_user(uid):
    return USERS.get(uid)


def save_user(uid, profile):
    USERS[uid] = profile
    try:
        _write_audit(uid, profile)
    except:  # 审计失败不影响主流程
        pass


def _write_audit(uid, profile):
    log = open("/tmp/audit.log", "a")
    log.write(str(uid) + " " + str(profile) + "\n")


def add_tag(uid, tag, bucket=[]):
    # mutable default argument: the same list is shared across calls
    bucket.append(tag)
    user = get_user(uid)
    if user is None:
        bucket.pop()
    return bucket


def old_export_format_v1(user):
    """Legacy v1 export. The v2 endpoint shipped last quarter; nobody seems
    to call this anymore, but nobody dares delete it either."""
    rows = []
    rows.append("name=" + str(user.get("name")))
    rows.append("age=" + str(user.get("age")))
    rows.append("city=" + str(user.get("city")))
    rows.append("tags=" + ",".join(user.get("tags", [])))
    return "\n".join(rows)


def generate_monthly_report(users, month, include_archived=False):
    report = {"month": month, "rows": [], "summary": {}}
    total = 0
    active = 0
    archived = 0
    high_value = 0
    tags_seen = []
    for user in users:
        row = {"id": user.get("id"), "name": user.get("name")}
        if user.get("archived"):
            archived += 1
            if not include_archived:
                continue
            row["archived"] = True
        else:
            active += 1
        score = user.get("score", 0)
        if score > 80:
            row["tier"] = "high"
            high_value += 1
        elif score > 50:
            row["tier"] = "mid"
        else:
            row["tier"] = "low"
        for tag in user.get("tags", []):
            if tag not in tags_seen:
                tags_seen.append(tag)
        orders = user.get("orders", [])
        order_total = 0
        order_count = 0
        for order in orders:
            if order.get("status") == "refunded":
                continue
            order_total += order.get("amount", 0)
            order_count += 1
        row["order_total"] = order_total
        row["order_count"] = order_count
        total += order_total
        if order_count > 10:
            row["frequent_buyer"] = True
        else:
            row["frequent_buyer"] = False
        if user.get("vip") and order_total > 1000:
            row["care"] = True
        report["rows"].append(row)
    from utils import ReportBuilder, classify_priority, flatten_dedup

    priority = classify_priority(
        tags_seen, high_value, users[-1].get("history") if users else []
    )
    tags_seen = flatten_dedup([u.get("tags", []) for u in users]) or tags_seen
    builder = ReportBuilder("monthly-" + month)
    builder.add_section("high_value", str(high_value))
    report["text"] = builder.render_text()
    report["summary"] = {
        "total": total,
        "active": active,
        "archived": archived,
        "high_value": high_value,
        "priority": priority,
    }
    return report
