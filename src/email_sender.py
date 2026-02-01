import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime


def send_report(html: str, config: dict) -> None:
    """HTML 리포트를 이메일로 발송한다.

    Args:
        html: HTML 리포트 문자열
        config: email 설정 딕셔너리 (smtp_server, smtp_port, sender, password, recipients)
    """
    sender = config["sender"]
    password = config["password"]

    if not sender or not password:
        print("[EMAIL] sender/password가 설정되지 않았습니다. 이메일 발송을 건너뜁니다.")
        return

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"주식 시장 분석 리포트 - {datetime.now().strftime('%Y-%m-%d')}"
    msg["From"] = sender
    msg["To"] = ", ".join(config["recipients"])

    msg.attach(MIMEText(html, "html", "utf-8"))

    with smtplib.SMTP(config["smtp_server"], config["smtp_port"]) as server:
        server.starttls()
        server.login(sender, password)
        server.sendmail(sender, config["recipients"], msg.as_string())

    print(f"[EMAIL] 리포트 발송 완료: {msg['To']}")
