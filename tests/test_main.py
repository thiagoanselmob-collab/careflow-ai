from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_read_root():
    """
    Test that GET / returns the correct status code and message.
    """
    response = client.get("/")
    assert response.status_code == 200
    assert response.json() == {"message": "Welcome to CareFlow AI Backend"}


def test_health_check():
    """
    Test that GET /health returns 200 OK and status ok.
    """
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_lifespan():
    """
    Test application lifespan hook startup and shutdown logic.
    """
    with TestClient(app) as test_client:
        response = test_client.get("/health")
        assert response.status_code == 200
