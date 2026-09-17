"""Demo web module.

The route decorator registers functions as HTTP handlers implicitly —
they are never called directly in code. Debtscope must NOT flag them as
dead code (this is the anti-false-positive fixture).
"""


from user_service import add_tag, generate_monthly_report, save_user


def route(path):
    def decorator(fn):
        fn.route_path = path
        return fn

    return decorator


@route("/health")
def health_check():
    return {"status": "ok"}


@route("/users")
def list_users(request):
    return {"users": _load_users(), "request_id": request.id}


@route("/users/save")
def save_user_endpoint(request):
    save_user(request.uid, request.profile)
    return {"ok": True}


@route("/users/tag")
def tag_user_endpoint(request):
    add_tag(request.uid, request.tag)
    return {"ok": True}


@route("/reports/monthly")
def monthly_report_endpoint(request):
    return generate_monthly_report(request.users, request.month)


def _load_users():
    # referenced by list_users -> not dead
    return [{"id": 1, "name": "alice"}, {"id": 2, "name": "bob"}]
