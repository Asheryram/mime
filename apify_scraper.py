import requests
import time
import json
from config import config

class ApifyScraper:
    def __init__(self):
        self.token = config.get("APIFY_API_TOKEN")
        if not self.token:
            raise ValueError("APIFY_API_TOKEN is missing in environment.")
            
    def run_contact_scraper(self, urls, max_pages=10, depth=1):
        """
        Runs vdrmota/contact-info-scraper actor on Apify for a list of URLs
        and returns the results containing scraped emails, phones, and socials.
        """
        if not urls:
            return []
            
        actor_url = f"https://api.apify.com/v2/acts/vdrmota~contact-info-scraper/runs?token={self.token}"
        
        start_urls = [{"url": u} for u in urls]
        
        payload = {
            "startUrls": start_urls,
            "maxRequestsPerStartUrl": max_pages,
            "maxDepth": depth,
            "proxyConfiguration": {
                "useApifyProxy": True
            }
        }
        
        headers = {
            "Content-Type": "application/json"
        }
        
        print(f"[Apify] Starting run for {len(urls)} URLs...")
        try:
            response = requests.post(actor_url, json=payload, headers=headers)
            if response.status_code != 201:
                print(f"[Apify] Failed to start run: {response.text}")
                return []
                
            run_data = response.json()["data"]
            run_id = run_data["id"]
            dataset_id = run_data["defaultDatasetId"]
            print(f"[Apify] Run started. Run ID: {run_id}, Dataset ID: {dataset_id}")
            
            # Polling loop
            status_url = f"https://api.apify.com/v2/actor-runs/{run_id}?token={self.token}"
            max_attempts = 30
            for attempt in range(max_attempts):
                time.sleep(10)
                status_res = requests.get(status_url)
                if status_res.status_code == 200:
                    status = status_res.json()["data"]["status"]
                    print(f"[Apify] Attempt {attempt+1}/{max_attempts}: Status is {status}")
                    if status in ["SUCCEEDED", "FAILED", "ABORTED", "TIMED-OUT"]:
                        if status != "SUCCEEDED":
                            print(f"[Apify] Run ended with status: {status}")
                        break
                else:
                    print(f"[Apify] Error checking run status: {status_res.text}")
            
            # Fetch results
            print(f"[Apify] Fetching dataset items from {dataset_id}...")
            items_url = f"https://api.apify.com/v2/datasets/{dataset_id}/items?token={self.token}"
            items_res = requests.get(items_url)
            if items_res.status_code == 200:
                return items_res.json()
            else:
                print(f"[Apify] Failed to fetch items: {items_res.text}")
                return []
                
        except Exception as e:
            print(f"[Apify] Exception during run: {e}")
            return []
