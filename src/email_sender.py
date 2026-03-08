import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime

from dotenv import load_dotenv

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
        print("[EMAIL] 이메일 인증정보가 설정되지 않았습니다. 발송을 건너뜁니다.")
        print("[EMAIL] .env 파일에 EMAIL_SENDER, EMAIL_PASSWORD를 설정하거나")
        print("[EMAIL] config/settings.yaml에 sender, password를 입력하세요.")
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
        print(f"[EMAIL] 리포트 발송 완료: {msg['To']}")
    except smtplib.SMTPAuthenticationError:
        print("[EMAIL] 인증 실패: EMAIL_SENDER, EMAIL_PASSWORD를 확인하세요.")
    except smtplib.SMTPConnectError:
        print(f"[EMAIL] 연결 실패: {config['smtp_server']}:{config['smtp_port']} 에 접속할 수 없습니다.")
    except smtplib.SMTPException as e:
        print(f"[EMAIL] 발송 실패: {e}")
    except OSError as e:
        print(f"[EMAIL] 네트워크 오류: {e}")
