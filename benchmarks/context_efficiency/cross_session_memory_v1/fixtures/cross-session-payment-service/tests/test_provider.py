from src.provider import MockPaymentProvider


def test_mock_provider_records_the_development_amount() -> None:
    result = MockPaymentProvider().charge("19.99")
    assert result == {"status": "fixture", "amount": "19.99"}
