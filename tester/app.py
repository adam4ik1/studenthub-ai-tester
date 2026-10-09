import json
import os
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse

from flask import Flask, render_template, request, send_from_directory
from playwright.sync_api import sync_playwright

BASE_DIR = Path(__file__).resolve().parent
SCREENSHOT_DIR = BASE_DIR / "static" / "screenshots"
REPORT_DIR = BASE_DIR / "reports"
SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
REPORT_DIR.mkdir(parents=True, exist_ok=True)

app = Flask(__name__)

def ai_explanation(results):
    """Use OpenAI if configured; always fall back to deterministic explanations."""
    fallback = {
        "task_creation": "The Add task form was submitted, but the task count did not increase. Check the submit event handler and ensure it creates and appends a new list item.",
        "email_validation": "The registration form accepted a malformed email. Use an email input and/or validate the address before displaying success.",
        "resources_navigation": "The Resources link returned an HTTP error. Check its href and make sure the destination route exists."
    }
    key = os.getenv("OPENAI_API_KEY")
    if not key:
        return {r["id"]: fallback.get(r["id"], "Review the failed assertion and the screenshot.") for r in results}
    try:
        from openai import OpenAI
        client = OpenAI(api_key=key)
        model = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")
        prompt = (
            "You are a careful junior web QA assistant. For each failed website test, explain "
            "the likely cause and one practical fix in plain English. Do not claim certainty "
            "beyond the evidence. Return a JSON object mapping test id to explanation. Results:\n"
            + json.dumps(results, ensure_ascii=False)
        )
        response = client.responses.create(model=model, input=prompt)
        text = response.output_text.strip()
        match = re.search(r"\{.*\}", text, re.S)
        parsed = json.loads(match.group(0) if match else text)
        return {r["id"]: str(parsed.get(r["id"], fallback.get(r["id"], "Review the failed assertion."))) for r in results}
    except Exception as exc:
        print("AI API unavailable; using fallback explanations:", exc)
        return {r["id"]: fallback.get(r["id"], "Review the failed assertion.") for r in results}

def run_tests(target_url):
    target_url = target_url.rstrip("/")
    parsed = urlparse(target_url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError("Enter a complete URL, e.g. http://127.0.0.1:5000")
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    results = []
    console_errors = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1365, "height": 900})
        page.on("pageerror", lambda error: console_errors.append(str(error)))

        # Test 1: add-task button must create a new list item.
        item_count_before = None
        try:
            page.goto(urljoin(target_url + "/", "tasks"), wait_until="networkidle")
            page.screenshot(path=str(SCREENSHOT_DIR / f"{stamp}_tasks_before.png"), full_page=True)
            item_count_before = page.locator("#task-list li").count()
            page.locator("#task-input").fill("QA demo task")
            page.get_by_role("button", name="Add task").click()
            page.wait_for_timeout(200)
            item_count_after = page.locator("#task-list li").count()
            passed = item_count_after == item_count_before + 1
            results.append({
                "id": "task_creation", "name": "Task creation",
                "status": "PASS" if passed else "FAIL",
                "details": f"Task count before: {item_count_before}; after: {item_count_after}.",
                "screenshot": f"{stamp}_task_creation.png"
            })
            page.screenshot(path=str(SCREENSHOT_DIR / f"{stamp}_task_creation.png"), full_page=True)
        except Exception as exc:
            results.append({"id":"task_creation","name":"Task creation","status":"FAIL","details":str(exc),"screenshot":None})

        # Test 2: malformed email must not produce a success message.
        try:
            page.goto(urljoin(target_url + "/", "register"), wait_until="networkidle")
            page.locator("#full-name").fill("QA Student")
            page.locator("#email").fill("student123")
            page.get_by_role("button", name="Create account").click()
            page.wait_for_timeout(150)
            message = page.locator("#register-message").inner_text()
            accepted = "successfully" in message.lower()
            passed = not accepted
            results.append({
                "id":"email_validation", "name":"Email validation",
                "status":"PASS" if passed else "FAIL",
                "details": f"Submitted 'student123'. Form message: {message or '(no message)'}.",
                "screenshot": f"{stamp}_email_validation.png"
            })
            page.screenshot(path=str(SCREENSHOT_DIR / f"{stamp}_email_validation.png"), full_page=True)
        except Exception as exc:
            results.append({"id":"email_validation","name":"Email validation","status":"FAIL","details":str(exc),"screenshot":None})

        # Test 3: Resources navigation must return a successful HTTP response.
        try:
            page.goto(target_url, wait_until="networkidle")
            link = page.get_by_role("link", name="Resources")
            href = link.get_attribute("href")
            destination = urljoin(target_url + "/", href or "")
            response = page.goto(destination, wait_until="networkidle")
            status_code = response.status if response else 0
            passed = 200 <= status_code < 400
            results.append({
                "id":"resources_navigation", "name":"Resources navigation",
                "status":"PASS" if passed else "FAIL",
                "details": f"Destination: {destination}; HTTP status: {status_code}.",
                "screenshot": f"{stamp}_resources_navigation.png"
            })
            page.screenshot(path=str(SCREENSHOT_DIR / f"{stamp}_resources_navigation.png"), full_page=True)
        except Exception as exc:
            results.append({"id":"resources_navigation","name":"Resources navigation","status":"FAIL","details":str(exc),"screenshot":None})

        browser.close()

    explanations = ai_explanation(results)
    for result in results:
        result["explanation"] = explanations.get(result["id"], "")
    report = {
        "run_id": stamp,
        "target_url": target_url,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "summary": {
            "total": len(results),
            "passed": sum(r["status"] == "PASS" for r in results),
            "failed": sum(r["status"] == "FAIL" for r in results)
        },
        "javascript_page_errors": console_errors,
        "results": results
    }
    report_path = REPORT_DIR / f"report_{stamp}.json"
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    report["report_file"] = report_path.name
    return report

@app.route("/", methods=["GET", "POST"])
def dashboard():
    report = None
    error = None
    target_url = request.form.get("target_url", "http://127.0.0.1:5000")
    if request.method == "POST":
        try:
            report = run_tests(target_url)
        except Exception as exc:
            error = str(exc)
    return render_template("dashboard.html", report=report, error=error, target_url=target_url)

@app.route("/screenshots/<path:filename>")
def screenshots(filename):
    return send_from_directory(SCREENSHOT_DIR, filename)

@app.route("/reports/<path:filename>")
def reports(filename):
    return send_from_directory(REPORT_DIR, filename, as_attachment=True)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5001))
    app.run(host="0.0.0.0", port=port, debug=False)
