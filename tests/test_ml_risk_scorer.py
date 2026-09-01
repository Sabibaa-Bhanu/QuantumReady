"""
Unit tests for Finding-Level ML Risk Scorer and Model Info endpoint.
"""
import pytest
import ml_risk_scorer
import app


def test_model_info_structure():
    """Verify /model-info returns required metadata fields."""
    info = ml_risk_scorer.get_model_info()
    assert info['status'] == 'ready'
    assert 'training_samples' in info
    assert 'cv_accuracy_percent' in info
    assert 'feature_names' in info
    assert 'top_feature_importances' in info
    assert info['training_samples'] > 100


def test_finding_scoring_rsa_critical():
    """Verify RSA finding receives Critical ML priority and explanation."""
    meta = ml_risk_scorer.score_finding(
        vuln_type="RSA",
        line_content="key = RSA.generate(2048)",
        surrounding_code="def authenticate_user():",
        frequency=3
    )
    assert meta['ml_priority'] == 'Critical'
    assert meta['ml_risk_score'] >= 80
    assert 'ML-KEM' in meta['risk_rationale'] or 'Shor' in meta['risk_rationale']


def test_finding_scoring_md5_high():
    """Verify MD5 finding receives High ML priority."""
    meta = ml_risk_scorer.score_finding(
        vuln_type="MD5",
        line_content="hashlib.md5(password.encode())",
        surrounding_code="def hash_password(password):",
        frequency=2
    )
    assert meta['ml_priority'] == 'High'
    assert meta['usage_context'] in ['authentication', 'hashing']
    assert 'collision' in meta['risk_rationale'].lower()


def test_model_info_api_endpoint():
    """Test /model-info and /api/model-info Flask routes."""
    client = app.app.test_client()
    resp = client.get('/model-info')
    assert resp.status_code == 200
    data = resp.get_json()
    assert data['status'] == 'ready'
    assert data['model_type'] == 'RandomForestClassifier'
