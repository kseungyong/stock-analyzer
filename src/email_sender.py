import logging
import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime

from dotenv import load_dotenv

logger = logging.getLogger(__name__)

# 환경변수 로드
load_dotenv()


def send_report(html: str, config: dict) -> None:
    """HTML 리포트를 이메일로 발송한다.

    Args:
        html: HTML 리포트 문자열
        config: email 설정 딕셔너리 (smtp_server, smtp_port, sender, password, recipients)

    Note:
        이메일 인증정보는 환경변수(.env)에서 우선 로드합니다:
        - EMAIL_SENDER: 발신자 이메일
        - EMAIL_PASSWORD: 앱 비밀번호
    """
    # 환경변수 우선, 없으면 config 사용
    sender = os.getenv("EMAIL_SENDER") or config.get("sender", "")
    password = os.getenv("EMAIL_PASSWORD") or config.get("password", "")

    if not sender or not password:
        logger.warning("이메일 인증정보 없음 — 발송 건너뜀. .env에 EMAIL_SENDER/EMAIL_PASSWORD를 설정하세요.")
        return

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"주식 시장 분석 리포트 - {datetime.now().strftime('%Y-%m-%d')}"
    msg["From"] = sender
    msg["To"] = ", ".join(config["recipients"])

    msg.attach(MIMEText(html, "html", "utf-8"))

    try:
        with smtplib.SMTP(config["smtp_server"], config["smtp_port"]) as server:
            server.starttls()
            server.login(sender, password)
            server.sendmail(sender, config["recipients"], msg.as_string())
        logger.info("리포트 발송 완료: %s", msg["To"])
    except smtplib.SMTPAuthenticationError:
        logger.error("이메일 인증 실패: EMAIL_SENDER, EMAIL_PASSWORD를 확인하세요.")
    except smtplib.SMTPConnectError:
        logger.error("이메일 연결 실패: %s:%s", config["smtp_server"], config["smtp_port"])
    except smtplib.SMTPException as e:
        logger.error("이메일 발송 실패: %s", e)
    except OSError as e:
        logger.error("이메일 네트워크 오류: %s", e)
