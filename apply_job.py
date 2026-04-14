import os
import sys
import json
import time
import docx
import argparse
import dotenv
from pathlib import Path
from pypdf import PdfReader
from typing import Dict, Any

from google import genai
from google.genai import types

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from webdriver_manager.chrome import ChromeDriverManager
from selenium.webdriver.support.ui import WebDriverWait


# Load environment variables from .env file
dotenv.load_dotenv()

# Configure Gemini Client
API_KEY = os.getenv("GEMINI_API_KEY")
MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

if API_KEY:
    client = genai.Client(api_key=API_KEY)
else:
    print("Warning: GEMINI_API_KEY not found in environment variables or .env file.")
    client = None


def extract_cv_text(file_path: str) -> str:
    """Extract text from PDF or DOCX file."""
    path = Path(file_path)
    if path.suffix.lower() == ".pdf":
        reader = PdfReader(file_path)
        text = ""
        for page in reader.pages:
            text += page.extract_text() + "\n"
        return text
    elif path.suffix.lower() == ".docx":
        doc = docx.Document(file_path)
        return "\n".join([p.text for p in doc.paragraphs])
    else:
        raise ValueError("Unsupported file format. Please provide .pdf or .docx")


def get_cv_data(cv_text: str) -> Dict[str, str]:
    """Use Gemini to extract structured info from CV text."""
    if not client:
        return {}

    prompt = f"""
    Extract the following information from the CV text provided below. 
    Return ONLY a JSON object with these keys: 
    full_name, email, phone_number, linkedin_url, github_url, portfolio_url, current_role, summary.
    If any field is missing, use an empty string.

    CV TEXT:
    {cv_text}
    """

    try:
        response = client.models.generate_content(
            model=MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(response_mime_type="application/json"),
        )
        return json.loads(response.text)
    except Exception as e:
        print(f"Error parsing Gemini response for CV data: {e}")
        # Fallback to older method if JSON mode fails or is unsupported
        try:
            response = client.models.generate_content(model=MODEL, contents=prompt)
            text = response.text.strip()
            if "```json" in text:
                text = text.split("```json")[1].split("```")[0].strip()
            return json.loads(text)
        except:
            return {}


def get_form_mapping(driver, cv_data: Dict[str, str]) -> Dict[str, Any]:
    """Analyze the page and return a mapping of field info to CSS selectors."""
    if not client:
        return {}

    elements = driver.find_elements(By.CSS_SELECTOR, "input, textarea, select, button, [role='button'], [type='button']")

    element_list = []
    elements_by_index = {}

    for i, el in enumerate(elements):
        try:
            is_file_input = el.tag_name == "input" and el.get_attribute("type") == "file"
            if not el.is_displayed() and not is_file_input:
                continue

            tag = el.tag_name
            type_attr = el.get_attribute("type") or ""
            name_attr = el.get_attribute("name") or ""
            id_attr = el.get_attribute("id") or ""
            class_attr = el.get_attribute("class") or ""
            text = el.text.strip()
            placeholder = el.get_attribute("placeholder") or ""

            if id_attr:
                selector = f"#{id_attr}"
            elif name_attr:
                selector = f"{tag}[name='{name_attr}']"
            else:
                selector = f"css_index_{i}"

            elements_by_index[selector] = el

            element_list.append(
                {
                    "index": i,
                    "tag": tag,
                    "type": type_attr,
                    "name": name_attr,
                    "id": id_attr,
                    "class": class_attr,
                    "text": text,
                    "placeholder": placeholder,
                    "selector": selector,
                }
            )
        except:
            continue

    prompt = f"""
    Below is a list of interactive HTML elements from a job application page.
    Match these elements to the required CV data fields: {list(cv_data.keys())} 
    and identify which element is for the CV/Resume file upload and which is the SUBMIT button.

    CV DATA FOR REFERENCE:
    {json.dumps(cv_data, indent=2)}

    ELEMENTS:
    {json.dumps(element_list, indent=2)}

    Return ONLY a JSON object with this structure:
    {{
        "field_mappings": {{ "field_key": "selector", ... }},
        "cv_upload_selector": "selector",
        "submit_button_selector": "selector"
    }}
    Important: Use the EXACT 'selector' string from the provided elements list.
    """

    try:
        response = client.models.generate_content(
            model=MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(response_mime_type="application/json"),
        )
        mapping = json.loads(response.text)
        return {"mapping": mapping, "elements": elements_by_index}
    except Exception as e:
        print(f"Error parsing Gemini response for form mapping: {e}")
        return {}


def apply(url: str, cv_path: str, auto_submit: bool = False, headless: bool = True):
    """Main function to automate the application."""
    print(f"Reading CV: {cv_path}")
    cv_text = extract_cv_text(cv_path)
    print("Extracting data from CV using Gemini...")
    cv_data = get_cv_data(cv_text)

    print(f"Opening browser to: {url}")
    chrome_options = Options()
    if headless:
        chrome_options.add_argument("--headless=new")
    driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=chrome_options)

    try:
        driver.get(url)
        wait = WebDriverWait(driver, 20)
        time.sleep(5)

        def analyze_context():
            print("Analyzing current context...")
            result = get_form_mapping(driver, cv_data)
            if result and result.get("mapping"):
                m = result["mapping"]
                if m.get("field_mappings") or m.get("cv_upload_selector"):
                    return result
            return None

        result = analyze_context()

        if not result:
            iframes = driver.find_elements(By.TAG_NAME, "iframe")
            print(f"Found {len(iframes)} iframes. Searching in iframes...")
            for i, frame in enumerate(iframes):
                driver.switch_to.default_content()
                driver.switch_to.frame(frame)
                print(f"Checking iframe {i}...")
                result = analyze_context()
                if result:
                    print(f"Form found in iframe {i}.")
                    break

        if not result:
            print("Failed to map form fields in any context. Exiting.")
            return

        mapping = result["mapping"]
        elements_by_index = result["elements"]
        print(f"Form Mapping: {json.dumps(mapping, indent=2)}")

        def find_el(selector):
            if selector in elements_by_index:
                return elements_by_index[selector]
            try:
                return driver.find_element(By.CSS_SELECTOR, selector)
            except:
                return None

        print("Filling form fields...")
        field_mappings = mapping.get("field_mappings", {})
        for field, selector in field_mappings.items():
            if field in cv_data and cv_data[field]:
                element = find_el(selector)
                if element:
                    try:
                        element.clear()
                        element.send_keys(cv_data[field])
                        print(f"  - Filled {field} into {selector}")
                    except Exception as e:
                        print(f"  - Error filling {field}: {e}")

        cv_selector = mapping.get("cv_upload_selector")
        if cv_selector:
            upload_el = find_el(cv_selector)
            if upload_el:
                try:
                    abs_cv_path = str(Path(cv_path).resolve())
                    upload_el.send_keys(abs_cv_path)
                    print(f"  - Uploaded CV to {cv_selector}")
                except Exception as e:
                    print(f"  - Error uploading CV: {e}")

        submit_selector = mapping.get("submit_button_selector")
        if submit_selector:
            if auto_submit:
                try:
                    submit_btn = find_el(submit_selector)
                    if submit_btn:
                        try:
                            submit_btn.click()
                        except:
                            try:
                                submit_btn.submit()
                            except:
                                driver.execute_script("arguments[0].click();", submit_btn)
                        print("Form submitted!")
                        time.sleep(5)
                except Exception as e:
                    print(f"Error submitting: {e}")
            else:
                print(f"Submit button found: {submit_selector}. Auto-submit is False.")
        else:
            print("Could not find submit button.")

    finally:
        print("Closing browser...")
        driver.quit()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Automate job application using Gemini.")
    parser.add_argument("url", help="Job application URL")
    parser.add_argument("cv", help="Path to CV file (.pdf or .docx)")
    parser.add_argument("--yes", action="store_true", help="Auto-submit without confirmation")
    parser.add_argument("--no-headless", action="store_true", help="Run with browser visible")
    args = parser.parse_args()

    if not API_KEY:
        print("Please set GEMINI_API_KEY environment variable.")
        sys.exit(1)

    apply(args.url, args.cv, args.yes, not args.no_headless)
