import os
import requests
import logging
import csv
import argparse
import datetime
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
from dotenv import load_dotenv

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

# Load environment variables
load_dotenv()


def load_pj_mapping(filepath):
    pj_map = {}
    if not filepath or not os.path.exists(filepath):
        logger.warning(f"STA_PJ_FILE not found at '{filepath}'. The PJ column will be blank.")
        return pj_map

    try:
        with open(filepath, mode='r', newline='', encoding='utf-8-sig') as csv_file:
            reader = csv.DictReader(csv_file)
            fieldnames = {fn.strip().lower(): fn for fn in reader.fieldnames} if reader.fieldnames else {}

            code_col_original = fieldnames.get('kode_stasiun')
            pj_col_original = fieldnames.get('pic')

            if not code_col_original or not pj_col_original:
                logger.error(f"Could not find 'kode_stasiun' or 'PIC' columns in {filepath}. PJ will be blank.")
                return pj_map

            for row in reader:
                code_val = row[code_col_original].strip().lower()
                pj_val = row[pj_col_original].strip()
                pj_map[code_val] = pj_val

        logger.info(f"Loaded {len(pj_map)} PIC mappings from {filepath}.")
    except Exception as e:
        logger.error(f"Failed to read the PJ file: {e}")

    return pj_map


def fetch_qc_summary(date_str):
    base_url = os.getenv("BASE_URL")
    api_key = os.getenv("API_KEY")

    if not base_url or not api_key:
        logger.error("BASE_URL and API_KEY must be set in your .env file.")
        return None

    url = f"{base_url}/qc/data/summary/{date_str}"
    headers = {
        "accept": "application/json",
        "Authorization": f"Bearer {api_key}",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }

    logger.info(f"Fetching data for date: {date_str}...")
    try:
        response = requests.get(url, headers=headers)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.HTTPError as http_err:
        logger.error(f"HTTP error for {date_str}: {http_err} | Reason: {response.text}")
        return None
    except Exception as err:
        logger.error(f"Unexpected error for {date_str}: {err}")
        return None


def get_result_rank(result_string):
    if not result_string:
        return 0
    val = result_string.strip().lower()
    ranks = {"baik": 4, "cukup baik": 3, "buruk": 2, "mati": 1}
    return ranks.get(val, 0)


def compare_and_export(start_date, end_date, pj_file_path, output_csv, changed_output_csv):
    pj_mapping = load_pj_mapping(pj_file_path)

    data_start = fetch_qc_summary(start_date)
    data_end = fetch_qc_summary(end_date)

    if data_start is None or data_end is None:
        logger.error("Could not fetch data for one or both dates. Aborting comparison.")
        return False

    dict_start = {item["code"]: {"result": item.get("result", "Unknown"), "details": item.get("details", "")} for item
                  in data_start}
    dict_end = {item["code"]: {"result": item.get("result", "Unknown"), "details": item.get("details", "")} for item in
                data_end}

    all_codes = set(dict_start.keys()).union(set(dict_end.keys()))

    logger.info(f"Generating main CSV file: {output_csv}...")
    logger.info(f"Generating changes-only CSV file: {changed_output_csv}...")

    with open(output_csv, mode='w', newline='', encoding='utf-8') as csv_file, \
            open(changed_output_csv, mode='w', newline='', encoding='utf-8') as changed_csv_file:

        writer = csv.writer(csv_file)
        changed_writer = csv.writer(changed_csv_file)

        header = [
            "sta_code", "PJ", "start_date", "end_date",
            "details_start_date", "details_end_date",
            "result_start_date", "result_end_date", "comparison"
        ]
        writer.writerow(header)
        changed_writer.writerow(header)

        for code in sorted(all_codes):
            safe_code_lookup = code.strip().lower()
            pj_val = pj_mapping.get(safe_code_lookup, "")

            start_info = dict_start.get(code, {"result": "Tidak Ada Data", "details": ""})
            end_info = dict_end.get(code, {"result": "Tidak Ada Data", "details": ""})

            res_start = start_info["result"]
            res_end = end_info["result"]

            det_start = str(start_info["details"]) if start_info["details"] is not None else ""
            det_end = str(end_info["details"]) if end_info["details"] is not None else ""

            det_start = det_start.replace(",", " |")
            det_end = det_end.replace(",", " |")

            rank_start = get_result_rank(res_start)
            rank_end = get_result_rank(res_end)

            if rank_start == 0 or rank_end == 0:
                comparison = "unknown"
            elif rank_end > rank_start:
                comparison = "better"
            elif rank_end < rank_start:
                comparison = "worse"
            else:
                comparison = "same"

            row_data = [
                code, pj_val, start_date, end_date,
                det_start, det_end, res_start.lower(), res_end.lower(), comparison
            ]

            writer.writerow(row_data)

            if comparison != "same":
                changed_writer.writerow(row_data)

    logger.info("Comparison completed successfully!")
    return True


def send_email_with_attachments(subject, body, recipients, attachments):
    smtp_server = os.getenv("SMTP_SERVER")
    smtp_port = os.getenv("SMTP_PORT", 587)
    smtp_user = os.getenv("SMTP_USERNAME")
    smtp_pass = os.getenv("SMTP_PASSWORD")
    sender = os.getenv("SENDER_EMAIL")

    if not all([smtp_server, smtp_user, smtp_pass, sender]):
        logger.error("Cannot send email. SMTP configuration is missing in .env file.")
        return

    msg = MIMEMultipart()
    msg['From'] = sender
    msg['To'] = ", ".join(recipients)
    msg['Subject'] = subject
    msg.attach(MIMEText(body, 'plain'))

    for filepath in attachments:
        if os.path.exists(filepath):
            try:
                with open(filepath, "rb") as file:
                    part = MIMEBase("application", "octet-stream")
                    part.set_payload(file.read())
                encoders.encode_base64(part)
                filename = os.path.basename(filepath)
                part.add_header("Content-Disposition", f"attachment; filename= {filename}")
                msg.attach(part)
            except Exception as e:
                logger.error(f"Could not attach file {filepath}: {e}")
        else:
            logger.warning(f"File {filepath} not found. Skipping attachment.")

    logger.info(f"Sending email to {len(recipients)} recipients...")
    try:
        server = smtplib.SMTP(smtp_server, int(smtp_port))
        server.starttls()
        server.login(smtp_user, smtp_pass)
        server.send_message(msg)
        server.quit()
        logger.info("Email sent successfully!")
    except Exception as e:
        logger.error(f"Failed to send email: {e}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Compare QC summary data between two dates.")

    parser.add_argument("--start_date",
                        help="The baseline date (e.g., 2026-08-09). If provided, --end_date is also required.")
    parser.add_argument("--end_date",
                        help="The target date to compare against (e.g., 2026-08-10). If provided, --start_date is also required.")
    parser.add_argument("--emails", help="Comma-separated list of email addresses to override defaults in .env.")
    parser.add_argument("--output_dir", help="Directory path to save the CSV files. Overrides OUTPUT_DIR in .env.")

    args = parser.parse_args()

    if bool(args.start_date) != bool(args.end_date):
        parser.error("You must provide BOTH --start_date and --end_date, or NEITHER to use defaults.")

    if not args.start_date and not args.end_date:
        today = datetime.date.today()
        end_date_obj = today - datetime.timedelta(days=1)
        start_date_obj = today - datetime.timedelta(days=2)

        final_start_date = start_date_obj.strftime("%Y-%m-%d")
        final_end_date = end_date_obj.strftime("%Y-%m-%d")
        logger.info(f"No dates provided. Defaulting to start_date: {final_start_date}, end_date: {final_end_date}")
    else:
        final_start_date = args.start_date
        final_end_date = args.end_date

    # 1. Determine Output Directory (Flag overrides .env)
    output_dir = args.output_dir if args.output_dir else os.getenv("OUTPUT_DIR", ".")

    # Ensure the output directory exists
    if output_dir and output_dir != ".":
        os.makedirs(output_dir, exist_ok=True)
        logger.info(f"Output directory set to: {output_dir}")

    # Retrieve the path to sta_pj.csv from .env
    pj_file_path = os.getenv("STA_PJ_FILE", "sta_pj.csv")

    # Set dynamic output file names and combine them with the output directory
    main_csv_name = f"qc_comparison_{final_start_date}_to_{final_end_date}.csv"
    changed_csv_name = f"qc_comparison_changes_{final_start_date}_to_{final_end_date}.csv"

    main_csv_path = os.path.join(output_dir, main_csv_name)
    changed_csv_path = os.path.join(output_dir, changed_csv_name)

    # Run the comparison
    success = compare_and_export(final_start_date, final_end_date, pj_file_path, output_csv=main_csv_path,
                                 changed_output_csv=changed_csv_path)

    # Determine which email list to use (Flag overrides .env)
    target_emails_str = args.emails if args.emails else os.getenv("DESTINATION_EMAILS")

    if success and target_emails_str:
        recipient_list = [email.strip() for email in target_emails_str.split(",") if email.strip()]
        if recipient_list:
            subject = f"QC Comparison Report: {final_start_date} to {final_end_date}"
            body = f"Hello,\n\nPlease find the attached QC comparison reports for the period from {final_start_date} to {final_end_date}.\n\n- The main file contains all stations.\n- The 'changes' file contains only stations where the status changed.\n\nRegards,\nAutomated QC System"

            attachments = [main_csv_path, changed_csv_path]

            send_email_with_attachments(subject, body, recipient_list, attachments)
        else:
            logger.warning("Target emails string was empty after parsing. No email sent.")