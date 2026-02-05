"""입력 검증 유틸리티."""
import re


def validate_stock_symbol(symbol: str) -> bool:
    """주식 심볼의 유효성을 검증한다.

    Args:
        symbol: 검증할 심볼 문자열

    Returns:
        유효한 심볼이면 True, 아니면 False

    허용 패턴:
        - 영문자, 숫자, 점(.), 하이픈(-), 언더스코어(_)만 허용
        - 길이: 1-20자
        - 예: AAPL, 005930.KS, MSFT, BRK-A
    """
    if not symbol or not isinstance(symbol, str):
        return False

    # 길이 제한
    if len(symbol) > 20 or len(symbol) < 1:
        return False

    # 허용된 문자만 포함 (영문자, 숫자, ., -, _)
    pattern = r'^[A-Za-z0-9._-]+$'
    return bool(re.match(pattern, symbol))


def sanitize_stock_symbol(symbol: str) -> str:
    """심볼을 정리하고 안전한 형태로 변환한다.

    Args:
        symbol: 원본 심볼 문자열

    Returns:
        정리된 심볼 (공백 제거, 대문자 변환)
    """
    return symbol.strip().upper()
