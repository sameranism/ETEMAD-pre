"""S08-013/014/015/022: direct server role and output-control enforcement."""
from fastapi.testclient import TestClient

from backend.app.dependencies import build_services
from backend.app.main import create_app
from backend.core.auth import Principal, issue_session
from backend.core.permissions import Grant


def test_role_endpoints_and_backend_switch_authority():
    services = build_services(environ={"ETEMAD_SESSION_KEY": "dashboard-fixture-key"})
    owner = Principal("owner-ui", "owner")
    staff = Principal("staff-ui", "staff")
    eic = Principal("eic-ui", "editor_in_chief")

    def headers(p):
        return {"Authorization": "Bearer " + issue_session(p, services.session_key)}

    key = "output_controls:global:controls-main"
    path = "/v1/ui/owner-dashboard/output-controls"
    change = {"key": "public_analysis", "enabled": True, "expected_revision": 0}
    with TestClient(create_app(services=services)) as client:
        assert client.get("/v1/ui/session").status_code == 401
        assert client.put(path, json=change, headers=headers(owner)).status_code == 403
        for p in (owner, staff, eic):
            for action in ("read", "update"):
                services.grants.grant(Grant(p.subject_id, p.role, "config", key, action))
        assert client.put(path, json=change, headers=headers(staff)).status_code == 403
        assert client.put(path, json=change, headers=headers(eic)).status_code == 403
        assert client.put(path, json={**change, "enabled": "true"}, headers=headers(owner)).status_code == 400
        assert client.put(path, json={**change, "actor_id": "other"}, headers=headers(owner)).status_code == 400
        assert not services.switches().enabled("public_analysis")
        response = client.put(path, json=change, headers=headers(owner))
        assert response.status_code == 200, response.text
        controls = response.json()["controls"]
        assert controls["revision"] == 1 and controls["values"]["public_analysis"] is True
        assert services.switches().enabled("public_analysis")
        assert not services.switches().enabled("studio") and not services.switches().enabled("publishing")
        assert client.put(path, json=change, headers=headers(owner)).status_code == 409
        services.grants.grant(Grant(owner.subject_id, owner.role, "private_report", "slot-5", "read"))
        services.grants.grant(Grant(staff.subject_id, staff.role, "private_report", "slot-5", "read"))
        for p in (owner, staff):
            assert client.get("/v1/analysis/daily-reports/5", headers=headers(p)).status_code == 404
            assert client.get("/v1/ui/editor-dashboard", headers=headers(p)).status_code == 403
        assert client.get("/v1/ui/owner-dashboard", headers=headers(eic)).status_code == 403
        assert client.get("/v1/ui/saved-views", headers=headers(owner)).status_code == 403
