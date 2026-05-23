"""log_filter — Python logging filter 로 시크릿 (API key 등) 자동 마스킹.

사용:
    from src.log_filter import install_secret_filter
    install_secret_filter([os.environ["DART_API_KEY"]])

이후 모든 logger 의 메시지/args 에서 해당 문자열이 자동으로 *** 로 치환.
URL query param, 예외 메시지, format 인자 등 모든 출력 경로 커버.
"""
from __future__ import annotations

import logging


class SecretFilter(logging.Filter):
    """logging.Filter 구현 — 로그 메시지에서 secret 문자열을 *** 로 치환."""

    def __init__(self, secrets: list[str]):
        super().__init__()
        # 빈 문자열은 무한 루프 위험 → 제외
        self._secrets = [s for s in secrets if s]

    def filter(self, record: logging.LogRecord) -> bool:
        # record.msg + args 모두 처리. getMessage() 호출 시점에 적용.
        msg = str(record.msg)
        for secret in self._secrets:
            msg = msg.replace(secret, "***")
        record.msg = msg
        if record.args:
            new_args = []
            for a in record.args:
                s = str(a)
                for secret in self._secrets:
                    s = s.replace(secret, "***")
                new_args.append(s)
            record.args = tuple(new_args)
        return True


def install_secret_filter(secrets: list[str]) -> None:
    """모든 root logger handler 에 SecretFilter 부착.

    Python logging 특성: filter 는 message origin logger 에만 적용.
    Handler 에 filter 부착하면 propagation 으로 도달한 모든 child logger 메시지도 처리.
    """
    flt = SecretFilter(secrets)
    root = logging.getLogger()
    # 기존 handler 모두에 부착
    for handler in root.handlers:
        handler.addFilter(flt)
    # 새로 추가될 handler 도 cover 하기 위해 root logger 자체에도 부착
    # (root.callHandlers() 가 filter chain 을 한 번 더 검사함)
    root.addFilter(flt)
