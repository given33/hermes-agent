from scripts.feature_alignment_inventory import declarations


def test_inventory_reads_rpc_and_prefixed_http_declarations_without_imports():
    surface = declarations('''
router = APIRouter(prefix="/api")
@router.get("/jobs")
async def jobs():
    raise RuntimeError("must not execute")
@method("profiles.list")
def profiles(rid, params):
    raise RuntimeError("must not execute")
''', "example.py")
    assert set(surface) == {"GET /api/jobs", "RPC profiles.list"}
    assert surface["GET /api/jobs"][0]["handler"] == "jobs"


def test_inventory_does_not_invent_dynamic_routes_from_comments_or_bodies():
    surface = declarations('''
# @router.get("/not-a-route")
@router.get(dynamic_path)
def dynamic():
    return "/not-a-route"
@router.post("/actual")
def actual():
    return "@method('not.a.method')"
''', "example.py")
    assert set(surface) == {"POST /actual"}
