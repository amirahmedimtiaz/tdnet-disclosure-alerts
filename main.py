import requests
from bs4 import BeautifulSoup
from datetime import datetime, timedelta
import pytz
import os
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# Configuration
STOCK_CODES = ["441a", "1450", "6658"]
TDNET_URL = "https://www.release.tdnet.info/onsf/TDJFSearch/TDJFSearch"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36",
    "Referer": "https://www.release.tdnet.info/onsf/TDJFSearch/I_head",
    "Content-Type": "application/x-www-form-urlencoded"
}

def get_yesterday_jst():
    jst = pytz.timezone('Asia/Tokyo')
    now_jst = datetime.now(jst)
    yesterday = now_jst - timedelta(days=1)
    return yesterday.strftime("%Y%m%d")

def fetch_disclosures(stock_code, date_str):
    payload = {
        "t0": date_str,
        "t1": date_str,
        "q": stock_code,
        "m": "0"
    }
    response = requests.post(TDNET_URL, headers=HEADERS, data=payload)
    response.raise_for_status()
    
    soup = BeautifulSoup(response.text, 'html.parser')
    table = soup.find('table', id='maintable')
    
    results = []
    if not table:
        return results

    rows = table.find_all('tr')
    for row in rows:
        cols = row.find_all('td')
        if len(cols) >= 4:
            code = cols[1].get_text(strip=True)
            link_tag = cols[3].find('a')
            url = "https://www.release.tdnet.info" + link_tag['href'] if link_tag and link_tag.get('href') else ""
            results.append({
                "time": cols[0].get_text(strip=True),
                "code": code,
                "company": cols[2].get_text(strip=True),
                "title": cols[3].get_text(strip=True),
                "url": url
            })
    return results

def send_email(all_results):
    smtp_server = os.environ.get("SMTP_SERVER")
    smtp_port = int(os.environ.get("SMTP_PORT", 587))
    smtp_user = os.environ.get("SMTP_USER")
    smtp_pass = os.environ.get("SMTP_PASSWORD")
    recipient = os.environ.get("RECIPIENT_EMAIL")

    if not all( [smtp_server, smtp_user, smtp_pass, recipient] ):
        print("Skipping email: SMTP credentials not fully configured.")
        return

    msg = MIMEMultipart()
    msg['From'] = smtp_user
    msg['To'] = recipient
    msg['Subject'] = f"TDnet Disclosure Alert - {datetime.now().strftime('%Y-%m-%d')}"

    body = "New disclosures found:\n\n"
    for item in all_results:
        body += f"Time: {item['time']}\n"
        body += f"Company: {item['company']} ({item['code']})\n"
        body += f"Title: {item['title']}\n"
        body += f"Link: {item['url']}\n"
        body += "-" * 30 + "\n"

    msg.attach(MIMEText(body, 'plain'))

    try:
        with smtplib.SMTP(smtp_server, smtp_port) as server:
            server.starttls()
            server.login(smtp_user, smtp_pass)
            server.send_message(msg)
        print("Email sent successfully.")
    except Exception as e:
        print(f"Failed to send email: {e}")

def main():
    date_str = get_yesterday_jst()
    print(f"Checking disclosures for date: {date_str}")
    
    all_results = []
    for code in STOCK_CODES:
        print(f"Fetching for {code}...")
        results = fetch_disclosures(code, date_str)
        all_results.extend(results)
    
    if all_results:
        print(f"Found {len(all_results)} disclosures. Sending email...")
        send_email(all_results)
    else:
        print("No new disclosures found.")

if __name__ == "__main__":
    main()
