import pandas as pd
import time
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from webdriver_manager.chrome import ChromeDriverManager
from selenium.webdriver.support.ui import Select, WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, NoSuchElementException

# Keywords for filtering relevant jobs
KEYWORDS = [
    "data analyst",
    "financial analyst",
    "data scientist",
    "data engineer",
    "business analyst",
    "data",
    "analytics",
    "bi analyst",
    "reporting analyst",
]

BASE_URL = "https://jobs.ge/en"


def setup_driver(headless=True):
    """Initialize Selenium WebDriver."""
    chrome_options = Options()
    if headless:
        chrome_options.add_argument("--headless=new")
    chrome_options.add_argument("--start-maximized")
    chrome_options.add_argument("--disable-blink-features=AutomationControlled")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")

    driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=chrome_options)
    return driver


def is_relevant(title):
    """Check if the job title matches any keyword."""
    if not title:
        return False
    title_lower = title.lower()
    return any(keyword in title_lower for keyword in KEYWORDS)


def get_job_metadata(driver: webdriver.Chrome) -> dict:
    """Extract job metadata like company, published date, and deadline."""
    search_texts = {"Provided By": "Company", "Published": "Published", "Deadline": "Deadline"}
    metadata = {}

    for search_text, key in search_texts.items():
        try:
            xpath = f"//*[contains(normalize-space(text()), '{search_text}')]/following-sibling::b"
            element = driver.find_element(By.XPATH, xpath)
            metadata[key] = element.text.strip()
        except NoSuchElementException:
            metadata[key] = "N/A"

    return metadata


def get_job_application(driver: webdriver.Chrome) -> str:
    """Extract job description text."""
    try:
        description = driver.find_element(By.XPATH, "//div[@id='job']//table[2]//tbody/tr[last()]")
        return description.text.strip() if description else "Description not found"
    except NoSuchElementException:
        return "Description not found"


def scrape_jobs(headless=True):
    """Scrape jobs from jobs.ge."""
    driver = setup_driver(headless=headless)
    jobs = []
    job_links = []

    try:
        print(f"Connecting to: {BASE_URL}")
        driver.get(BASE_URL)
        wait = WebDriverWait(driver, 15)

        # Select Category
        wait.until(EC.presence_of_element_located((By.NAME, "cid")))
        category = Select(driver.find_element(By.NAME, "cid"))
        category.select_by_visible_text("Finance, Statistics")

        # Select Location
        wait.until(EC.presence_of_element_located((By.NAME, "lid")))
        location = Select(driver.find_element(By.NAME, "lid"))
        location.select_by_visible_text("Tbilisi")

        # Give some time for the list to refresh
        time.sleep(3)

        # Find all job links first to avoid stale element exceptions
        elements = driver.find_elements(By.XPATH, "//a[contains(@href, '/en/?view=jobs&id=')]")

        # Store (title, href) pairs
        for elem in elements:
            try:
                title = elem.text.strip()
                href = elem.get_attribute("href")
                if title and href and is_relevant(title):
                    job_links.append((title, href))
            except Exception:
                continue

        # Remove duplicates from the list while preserving order
        unique_links = []
        seen_hrefs = set()
        for t, h in job_links:
            if h not in seen_hrefs:
                unique_links.append((t, h))
                seen_hrefs.add(h)

        print(f"Found {len(unique_links)} relevant job links. Starting detailed extraction...")

        # Process each collected link
        for title, href in unique_links:
            try:
                print(f"Processing: {title}")
                driver.get(href)
                # Wait for job detail to load
                wait.until(EC.presence_of_element_located((By.ID, "job")))

                metadata = get_job_metadata(driver)
                description = get_job_application(driver)

                jobs.append(
                    {
                        "Title": title,
                        "Company": metadata.get("Company", "N/A"),
                        "Published": metadata.get("Published", "N/A"),
                        "Deadline": metadata.get("Deadline", "N/A"),
                        "Application Link": href,
                        "Description": description,
                    }
                )
                # Anti-throttling
                time.sleep(1)
            except Exception as e:
                print(f"Error processing {href}: {e}")
                continue

    finally:
        driver.quit()

    return jobs


def save_to_csv(jobs, filename="jobs_ge_data_roles.csv"):
    """Save results to a CSV file."""
    if not jobs:
        print("No jobs to save.")
        return

    df = pd.DataFrame(jobs)
    # Ensure no duplicates in final output
    df.drop_duplicates(subset=["Application Link"], inplace=True)
    df.to_csv(filename, index=False, encoding="utf-8-sig")
    print(f"Saved {len(df)} jobs to {filename}")


def print_jobs(jobs):
    """Print jobs to the console."""
    if not jobs:
        print("\n=== NO RELEVANT JOBS FOUND ===\n")
        return

    print(f"\n=== FOUND {len(jobs)} DATA-RELATED JOBS ===\n")
    for job in jobs:
        print(f"Title: {job['Title']}")
        print(f"Company: {job['Company']}")
        print(f"Deadline: {job['Deadline']}")
        print(f"Link: {job['Application Link']}")
        print("-" * 60)


if __name__ == "__main__":
    found_jobs = scrape_jobs(headless=True)
    print_jobs(found_jobs)
    save_to_csv(found_jobs)
