import requests
from config import config

class ExaSearch:
    def __init__(self):
        self.api_key = config.get("EXA_API_KEY")
        if not self.api_key:
            raise ValueError("EXA_API_KEY is missing in environment.")
            
    def search_companies(self, query, num_results=15):
        """
        Searches for pages matching the query and returns a list of results.
        Each result contains title, url, author, highlights, text.
        """
        url = "https://api.exa.ai/search"
        headers = {
            "x-api-key": self.api_key,
            "Content-Type": "application/json"
        }
        
        payload = {
            "query": query,
            "type": "auto",
            "numResults": num_results,
            "contents": {
                "text": True
            }
        }
        
        print(f"[Exa] Searching for: '{query}'...")
        try:
            response = requests.post(url, json=payload, headers=headers)
            if response.status_code == 200:
                data = response.json()
                results = data.get("results", [])
                print(f"[Exa] Found {len(results)} search results.")
                return results
            else:
                print(f"[Exa] Search failed: {response.status_code} - {response.text}")
                return []
        except Exception as e:
            print(f"[Exa] Exception during search: {e}")
            return []
            
    def get_company_domains(self, results):
        """
        Helper to clean URLs and extract domain name and title.
        """
        from urllib.parse import urlparse
        companies = []
        for r in results:
            parsed = urlparse(r.get("url", ""))
            domain = f"{parsed.scheme}://{parsed.netloc}"
            companies.append({
                "name": r.get("title", "Unknown"),
                "url": r.get("url", ""),
                "domain": domain,
                "text": r.get("text", "")
            })
        return companies
