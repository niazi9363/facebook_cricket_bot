import os
import sys
import time
import json
import re
import requests
import threading
from http.server import SimpleHTTPRequestHandler, HTTPServer
from bs4 import BeautifulSoup
from dotenv import load_dotenv

# Ensure standard output uses UTF-8 encoding (prevents emoji print errors on Windows)
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')


# Load credentials from .env file
load_dotenv()

FB_PAGE_ID = os.getenv("FB_PAGE_ID")
if FB_PAGE_ID:
    FB_PAGE_ID = FB_PAGE_ID.strip()

FB_ACCESS_TOKEN = os.getenv("FB_ACCESS_TOKEN")
if FB_ACCESS_TOKEN:
    FB_ACCESS_TOKEN = FB_ACCESS_TOKEN.strip()

# File to store the last posted scores to avoid duplicate posts
LAST_POST_FILE = "last_post.json"

# Cricbuzz Headers
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

def get_live_match_links():
    """
    Scrapes Cricbuzz Live Scores page to find unique active match details.
    """
    url = "https://www.cricbuzz.com/cricket-match/live-scores"
    try:
        response = requests.get(url, headers=HEADERS, timeout=15)
        if response.status_code != 200:
            print(f"[-] Error loading Cricbuzz homepage: Status {response.status_code}")
            return []
        
        soup = BeautifulSoup(response.text, 'html.parser')
        links = soup.find_all('a', href=re.compile(r'/live-cricket-scores/'))
        
        matches = []
        seen_ids = set()
        
        for link in links:
            href = link.get('href')
            title_attr = link.get('title', '')
            if not href or not title_attr:
                continue
            
            # Extract unique Match ID from the URL
            match_id_match = re.search(r'/live-cricket-scores/(\d+)/', href)
            if match_id_match:
                match_id = match_id_match.group(1)
                if match_id not in seen_ids:
                    seen_ids.add(match_id)
                    
                    # Split title into Match Name and Status (e.g. "INDW vs ENGW, 1st T20I - Live")
                    parts = title_attr.split(" - ")
                    match_title = parts[0].strip()
                    match_status = parts[1].strip() if len(parts) > 1 else "Live"
                    
                    # Remove trailing whitespace or quotes
                    match_title = match_title.strip("\"' ")
                    match_status = match_status.strip("\"' ")
                    
                    matches.append({
                        "id": match_id,
                        "url": f"https://www.cricbuzz.com{href}",
                        "title": match_title,
                        "status": match_status
                    })
                    
        return matches
    except Exception as e:
        print(f"[-] Exception in get_live_match_links: {e}")
        return []


def scrape_match_details(match_info):
    """
    Scrapes the detail page of a specific match to get detailed team scores.
    """
    match_url = match_info["url"]
    match_title = match_info["title"]
    match_status = match_info["status"]
    
    try:
        response = requests.get(match_url, headers=HEADERS, timeout=15)
        if response.status_code != 200:
            return None
        
        soup = BeautifulSoup(response.text, 'html.parser')
        
        # Decompose/Remove tickers and headers to avoid picking up other matches' scores
        for ticker in soup.find_all('div', class_=re.compile(r'homePageCurrentMatchesIn|text-white bg-\[#4a4a4a\]|wb:block hidden')):
            ticker.decompose()
            
        # Extract potential team abbreviations/names from the title to match scores
        title_teams = []
        # Find uppercase words of length 2-5 (abbreviations like INDW, ENGW, RWA)
        title_teams.extend(re.findall(r'\b([A-Z]{2,5})\b', match_title))
        
        # Split by "vs" to get full team names (e.g. "Rwanda vs Sierra Leone")
        vs_parts = re.split(r'\s+vs\s+', match_title, flags=re.I)
        for part in vs_parts:
            # Clean up series names if appended (e.g. "Rwanda, 14th Match" -> "Rwanda")
            clean_part = part.split(',')[0].strip()
            if clean_part:
                title_teams.append(clean_part)
                
        # De-duplicate team keys (lowercase)
        team_keys = list(set([t.lower() for t in title_teams if len(t) > 1]))
        
        # 1. Search for live scores on page
        scores_found = []
        
        # Get all text in the page joined by spaces to handle Cricbuzz layout splitting
        page_text = soup.get_text(separator=' ')
        page_text = re.sub(r'\s+', ' ', page_text)
        
        # Match: TEAM_NAME SCORE (OVERS)
        pattern_score_slash = r'\b([A-Z]{2,5})\b\s+(\d+(?:\s*[-/]\s*\d+)?)\s*(?:\(\s*(\d+(?:\.\d+)?)\s*\))?'
        pattern_score_overs = r'\b([A-Z]{2,5})\b\s+(\d+)\s*(?:\(\s*(\d+(?:\.\d+)?)\s*\))'
        
        NON_TEAM_WORDS = {'crr', 'tar', 'req', 'ship', 'eco', 'avg', 'sr', 'ov', 'overs', 'runs', 'wkts', 'toss', 'win', 'loss'}
        
        # Parse slash scores
        for m in re.finditer(pattern_score_slash, page_text):
            team_name = m.group(1)
            score_val = m.group(2)
            overs_val = m.group(3)
            
            if team_name.lower() in NON_TEAM_WORDS:
                continue
                
            has_hyphen_or_slash = '-' in score_val or '/' in score_val
            has_overs = overs_val is not None
            
            if not (has_hyphen_or_slash or has_overs):
                continue
                
            score_cleaned = re.sub(r'\s+', '', score_val)
            overs_str = f"({overs_val} ov)" if overs_val else ""
            full_score = f"{team_name.upper()} {score_cleaned} {overs_str}".strip()
            
            if full_score not in scores_found:
                scores_found.append(full_score)
                
        # Parse overs scores (e.g. team has won or no wickets lost yet)
        for m in re.finditer(pattern_score_overs, page_text):
            team_name = m.group(1)
            score_val = m.group(2)
            overs_val = m.group(3)
            
            if team_name.lower() in NON_TEAM_WORDS:
                continue
                
            score_cleaned = re.sub(r'\s+', '', score_val)
            overs_str = f"({overs_val} ov)"
            full_score = f"{team_name.upper()} {score_cleaned} {overs_str}".strip()
            
            if full_score not in scores_found:
                scores_found.append(full_score)

        # 2. If no score found via body regex, check page title as fallback
        if not scores_found:
            page_title = soup.title.text.strip() if soup.title else ""
            title_score_matches = re.findall(r'\b[A-Z]{2,5}\b\s+\d+/\d+(?:\s*\(\d+(?:\.\d+)?\))?', page_title)
            for sm in title_score_matches:
                if sm not in scores_found:
                    scores_found.append(sm)

        # 3. Look for a more precise match status on the detail page
        # E.g. "Rwanda won by 8 wickets" is better than homepage's static "Complete"
        precise_status = ""
        status_keywords = ['need', 'won by', 'opt to', 'stumps', 'preview', 'delayed', 'trail by', 'lead by', 'innings', 'day']
        
        # Match only highly specific status containers
        status_divs = soup.find_all(['div', 'span', 'p'], class_=re.compile(r'cb-min-stts|cb-text-complete|cb-text-live|cb-text-preview|cb-text-abandoned|text-cbTxtLive|text-cbLive|text-cbSuccess'))
        for sd in status_divs:
            text = sd.text.strip()
            if 10 < len(text) < 150:
                text_lower = text.lower()
                if any(kw in text_lower for kw in status_keywords):
                    precise_status = text
                    break
                    
        # Update status if a more precise one is found
        final_status = precise_status if precise_status else match_status

        # Try to extract batsman and bowler details from next.js miniscore JSON
        batsmen = []
        bowlers = []
        partnership = None
        
        try:
            for script in soup.find_all('script'):
                if not script.string:
                    continue
                text = script.string
                if 'miniscore' in text:
                    idx = text.find('"miniscore"')
                    if idx == -1:
                        idx = text.find('\\"miniscore\\"')
                    
                    if idx != -1:
                        start_curly = text.find('{', idx)
                        if start_curly != -1:
                            brace_count = 0
                            in_quote = False
                            escape = False
                            for i in range(start_curly, len(text)):
                                char = text[i]
                                if char == '\\' and not escape:
                                    escape = True
                                    continue
                                if char == '"' and not escape:
                                    in_quote = not in_quote
                                if not in_quote:
                                    if char == '{':
                                        brace_count += 1
                                    elif char == '}':
                                        brace_count -= 1
                                        if brace_count == 0:
                                            json_str = text[start_curly:i+1]
                                            cleaned = json_str.replace('\\"', '"').replace('\\\\', '\\')
                                            data = json.loads(cleaned)
                                            
                                            # Parse Batsmen
                                            for b_key in ['batsmanStriker', 'batsmanNonStriker']:
                                                if b_key in data and data[b_key]:
                                                    b = data[b_key]
                                                    b_name = b.get('name')
                                                    b_runs = b.get('runs')
                                                    b_balls = b.get('balls')
                                                    if b_name and b_name != "$undefined" and b_name.strip():
                                                        star = "*" if b_key == 'batsmanStriker' else ""
                                                        batsmen.append(f"{b_name.strip()}{star} - {b_runs} ({b_balls})")
                                            
                                            # Parse Bowlers
                                            for b_key in ['bowlerStriker', 'bowlerNonStriker']:
                                                if b_key in data and data[b_key]:
                                                    b = data[b_key]
                                                    b_name = b.get('name')
                                                    b_overs = b.get('overs')
                                                    b_runs = b.get('runs')
                                                    b_wkts = b.get('wickets')
                                                    if b_name and b_name != "$undefined" and b_name.strip():
                                                        bowlers.append(f"{b_name.strip()} - {b_wkts}/{b_runs} ({b_overs})")
                                            
                                            # Parse Partnership
                                            if 'partnerShip' in data and data['partnerShip']:
                                                ps = data['partnerShip']
                                                p_runs = ps.get('runs')
                                                p_balls = ps.get('balls')
                                                if p_runs is not None and p_runs != "$undefined":
                                                    partnership = f"{p_runs} runs off {p_balls} balls"
                                                    
                                            # Parse precise team scores from inningsScoreList
                                            if 'matchScoreDetails' in data and data['matchScoreDetails']:
                                                score_list = data['matchScoreDetails'].get('inningsScoreList', [])
                                                if score_list:
                                                    team_scores = {}
                                                    for inn in score_list:
                                                        t_name = inn.get('batTeamName')
                                                        runs = inn.get('score')
                                                        wkts = inn.get('wickets')
                                                        decl = inn.get('isDeclared')
                                                        f_on = inn.get('isFollowOn')
                                                        
                                                        if t_name and runs is not None:
                                                            score_part = f"{runs}"
                                                            if wkts is not None and wkts < 10:
                                                                score_part = f"{runs}/{wkts}"
                                                            if decl:
                                                                score_part += " d"
                                                            if f_on:
                                                                score_part += " (f/o)"
                                                            
                                                            if t_name not in team_scores:
                                                                team_scores[t_name] = []
                                                            team_scores[t_name].append(score_part)
                                                    
                                                    json_scores = []
                                                    for t_name, parts in team_scores.items():
                                                        combined_parts = " & ".join(parts)
                                                        json_scores.append(f"{t_name} {combined_parts}")
                                                    
                                                    if json_scores:
                                                        scores_found = json_scores
                                            break
                                escape = False
        except Exception as json_err:
            print(f"[-] Error extracting miniscore JSON: {json_err}")

        return {
            "title": match_title,
            "scores": scores_found,
            "status": final_status,
            "url": match_url,
            "batsmen": batsmen,
            "bowlers": bowlers,
            "partnership": partnership
        }
    except Exception as e:
        print(f"[-] Exception scraping match {match_url}: {e}")
        return None

FLAG_MAPPING = {
    # India
    'IND': '🇮🇳', 'INDW': '🇮🇳', 'INDIA': '🇮🇳', 'IND-W': '🇮🇳',
    # Pakistan
    'PAK': '🇵🇰', 'PAKW': '🇵🇰', 'PAKISTAN': '🇵🇰', 'PAK-W': '🇵🇰',
    # New Zealand
    'NZ': '🇳🇿', 'NZW': '🇳🇿', 'NEW ZEALAND': '🇳🇿', 'NZ-W': '🇳🇿',
    # England
    'ENG': '🏴\u200d\u200d󠁢\u200d\u200d󠁥\u200d\u200d󠁮\u200d\u200d󠁧\u200d\u200d󠁿', 'ENGW': '🏴\u200d\u200d󠁢\u200d\u200d󠁥\u200d\u200d󠁮\u200d\u200d󠁧\u200d\u200d󠁿', 'ENGLAND': '🏴\u200d\u200d󠁢\u200d\u200d󠁥\u200d\u200d󠁮\u200d\u200d󠁧\u200d\u200d󠁿', 'ENG-W': '🏴\u200d\u200d󠁢\u200d\u200d󠁥\u200d\u200d󠁮\u200d\u200d󠁧\u200d\u200d󠁿',
    # Australia
    'AUS': '🇦🇺', 'AUSW': '🇦🇺', 'AUSTRALIA': '🇦🇺', 'AUS-W': '🇦🇺',
    # South Africa
    'SA': '🇿🇦', 'SAW': '🇿🇦', 'SOUTH AFRICA': '🇿🇦', 'SA-W': '🇿🇦',
    # West Indies
    'WI': '🌴', 'WIW': '🌴', 'WEST INDIES': '🌴', 'WI-W': '🌴',
    # Sri Lanka
    'SL': '🇱🇰', 'SLW': '🇱🇰', 'SRI LANKA': '🇱🇰', 'SL-W': '🇱🇰',
    # Bangladesh
    'BAN': '🇧🇩', 'BANW': '🇧🇩', 'BANGLADESH': '🇧🇩', 'BAN-W': '🇧🇩',
    # Afghanistan
    'AFG': '🇦🇫', 'AFGW': '🇦🇫', 'AFGHANISTAN': '🇦🇫', 'AFG-W': '🇦🇫',
    # Ireland
    'IRE': '🇮🇪', 'IREW': '🇮🇪', 'IRELAND': '🇮🇪', 'IRE-W': '🇮🇪',
    # Zimbabwe
    'ZIM': '🇿🇼', 'ZIMW': '🇿🇼', 'ZIMBABWE': '🇿🇼', 'ZIM-W': '🇿🇼',
    # Others
    'SCO': '🏴\u200d\u200d󠁢\u200d\u200d󠁳\u200d\u200d󠁣\u200d\u200d󠁴\u200d\u200d󠁿', 'SCOW': '🏴\u200d\u200d󠁢\u200d\u200d󠁳\u200d\u200d󠁣\u200d\u200d󠁴\u200d\u200d󠁿', 'SCOTLAND': '🏴\u200d\u200d󠁢\u200d\u200d󠁳\u200d\u200d󠁣\u200d\u200d󠁴\u200d\u200d󠁿',
    'NED': '🇳🇱', 'NEDW': '🇳🇱', 'NETHERLANDS': '🇳🇱',
    'NEP': '🇳🇵', 'NEPW': '🇳🇵', 'NEPAL': '🇳🇵',
    'OMA': '🇴🇲', 'OMAN': '🇴🇲',
    'UAE': '🇦🇪', 'UAEW': '🇦🇪', 'UNITED ARAB EMIRATES': '🇦🇪',
    'USA': '🇺🇸', 'USAW': '🇺🇸',
    'NAM': '🇳🇦', 'NAMIBIA': '🇳🇦',
    'CAN': '🇨🇦', 'CANADA': '🇨🇦',
    'PNG': '🇵🇬', 'PAPUA NEW GUINEA': '🇵🇬',
    'UGA': '🇺🇬', 'UGANDA': '🇺🇬',
    'RWA': '🇷🇼', 'RWANDA': '🇷🇼',
    'SLE': '🇸🇱', 'SIERRA LEONE': '🇸🇱',
    'KEN': '🇰🇪', 'KENYA': '🇰🇪',
    'BOT': '🇧🇼', 'BOTSWANA': '🇧🇼',
    'NGA': '🇳🇬', 'NIGERIA': '🇳🇬',
    'GHA': '🇬🇭', 'GHANA': '🇬🇭',
    'CMR': '🇨🇲', 'CAMEROON': '🇨🇲',
    'MWI': '🇲🇼', 'MALAWI': '🇲🇼',
    'MOZ': '🇲🇿', 'MOZAMBIQUE': '🇲🇿',
    'TAN': '🇹🇿', 'TANZANIA': '🇹🇿',
    'LES': '🇱🇸', 'LESOTHO': '🇱🇸',
    'MLI': '🇲🇱', 'MALI': '🇲🇱',
    'CIV': '🇨🇮', 'IVORY COAST': '🇨🇮',
    'JER': '🇯🇪', 'JERSEY': '🇯🇪',
    'GUE': '🇬🇬', 'GUERNSEY': '🇬🇬',
    'HK': '🇭🇰', 'HKG': '🇭🇰', 'HONG KONG': '🇭🇰',
    'MAS': '🇲🇾', 'MALAYSIA': '🇲🇾',
    'SGP': '🇸🇬', 'SINGAPORE': '🇸🇬',
    'THA': '🇹🇭', 'THAILAND': '🇹🇭',
    'KUW': '🇰🇼', 'KUWAIT': '🇰🇼',
    'BAH': '🇧🇭', 'BAHRAIN': '🇧🇭',
    'INA': '🇮🇩', 'INDONESIA': '🇮🇩',
    'MYA': '🇲🇲', 'MYANMAR': '🇲🇲',
}

def get_flag(team_name):
    clean_name = team_name.strip().upper()
    # Check direct match
    if clean_name in FLAG_MAPPING:
        return FLAG_MAPPING[clean_name]
    # Check if key is in name or vice versa
    for key, flag in FLAG_MAPPING.items():
        if len(key) > 2 and (key in clean_name or clean_name in key):
            return flag
    return "🏏"

def detect_match_format(title):
    title_lower = title.lower()
    if "test" in title_lower:
        return "🔴 **TEST MATCH** 🔴"
    elif "t20" in title_lower:
        return "⚡ **T20 MATCH** ⚡"
    elif "odi" in title_lower or "one day" in title_lower:
        return "📅 **ODI MATCH** 📅"
    elif "ipl" in title_lower or "t20 league" in title_lower or "psl" in title_lower:
        return "⚡ **T20 LEAGUE** ⚡"
    else:
        return "🏏 **LIVE CRICKET UPDATE** 🏏"

def add_flags_to_title(title):
    # Split by vs to extract team names
    parts = re.split(r'(\s+vs\.?\s+)', title, flags=re.I)
    if len(parts) >= 3:
        team1_raw = parts[0]
        team2_raw = parts[2]
        
        flag1 = get_flag(team1_raw)
        
        team2_parts = team2_raw.split(',')
        team2_name = team2_parts[0]
        flag2 = get_flag(team2_name)
        
        team1_with_flag = f"{team1_raw.strip()} {flag1}"
        team2_with_flag = f"{team2_name.strip()} {flag2}"
        if len(team2_parts) > 1:
            team2_with_flag += "," + ",".join(team2_parts[1:])
            
        return f"{team1_with_flag}{parts[1]}{team2_with_flag}"
    return title

def add_flag_to_score(score):
    m = re.match(r'^([A-Z0-9a-z\-]{2,5})\b', score)
    if m:
        team_code = m.group(1)
        flag = get_flag(team_code)
        if flag != "🏏":
            return f"{flag} {score}"
    return score

def format_score_post(match_info):
    """
    Formats the match details into a professional, eye-catching Facebook post layout.
    """
    title = match_info["title"]
    scores = match_info["scores"]
    status = match_info["status"]
    batsmen = match_info.get("batsmen", [])
    bowlers = match_info.get("bowlers", [])
    partnership = match_info.get("partnership")
    
    # Detect format header
    format_header = detect_match_format(title)
    
    # Add flags to title
    title_with_flags = add_flags_to_title(title)
    
    post_lines = [
        format_header,
        "━━━━━━━━━━━━━━━━━━━━",
        f"🏆 **Match:** {title_with_flags}",
        ""
    ]
    
    if scores:
        post_lines.append("📊 **Current Scores:**")
        for score in scores:
            score_with_flag = add_flag_to_score(score)
            post_lines.append(f"  👉 {score_with_flag}")
        post_lines.append("")
        
    if batsmen:
        post_lines.append("🏏 **Active Batters:**")
        for bat in batsmen:
            post_lines.append(f"  🏏 {bat}")
        post_lines.append("")
        
    if bowlers:
        post_lines.append("🎯 **Active Bowlers:**")
        for bowl in bowlers:
            post_lines.append(f"  🎳 {bowl}")
        post_lines.append("")
        
    if partnership:
        post_lines.append(f"🤝 **Partnership:** {partnership}")
        post_lines.append("")
        
    post_lines.append(f"💬 **Status:** {status}")
    post_lines.append("━━━━━━━━━━━━━━━━━━━━")
    post_lines.append("#Cricket #LiveScore #CricketUpdates #CricketFans")
    
    return "\n".join(post_lines)

def is_international_match(title):
    # Split by 'vs'
    parts = re.split(r'\s+vs\.?\s+', title, flags=re.I)
    if len(parts) < 2:
        return False
    
    team1 = parts[0].strip().upper()
    team2 = re.split(r',', parts[1])[0].strip().upper() # Extract team2 name before comma
    
    # Check if they match FLAG_MAPPING keys or values
    def is_country(team_name):
        if team_name in FLAG_MAPPING:
            return True
        for key in FLAG_MAPPING.keys():
            if len(key) > 2 and (key in team_name or team_name in key):
                return True
        return False
        
    return is_country(team1) and is_country(team2)

def get_squad_info(match_id):
    """
    Fetches the squad page for the match and returns a set of lowercase player names and a set of profile IDs.
    """
    squad_url = f"https://www.cricbuzz.com/cricket-match-squads/{match_id}"
    try:
        res = requests.get(squad_url, headers=HEADERS, timeout=15)
        if res.status_code != 200:
            return set(), set()
        soup = BeautifulSoup(res.text, 'html.parser')
        
        player_links = soup.find_all('a', href=re.compile(r'/profiles/(\d+)'))
        squad_ids = set()
        squad_names = set()
        for l in player_links:
            m = re.search(r'/profiles/(\d+)/', l['href'])
            if m:
                squad_ids.add(m.group(1))
            name = l.text.strip().lower()
            if name:
                squad_names.add(name)
                # Split by space and add individual parts (like last name)
                for part in name.split():
                    if len(part) > 2:
                        squad_names.add(part)
        return squad_ids, squad_names
    except Exception as e:
        print(f"[-] Error fetching squad info for {match_id}: {e}")
        return set(), set()

def get_match_preview_alert(match_id, match_title):
    """
    Scrapes the match preview article from Cricbuzz news tab, filters it using squads, and returns a formatted post.
    """
    squad_ids, squad_names = get_squad_info(match_id)
    
    news_url = f"https://www.cricbuzz.com/cricket-match-news/{match_id}"
    try:
        res = requests.get(news_url, headers=HEADERS, timeout=15)
        if res.status_code != 200:
            return None
        soup = BeautifulSoup(res.text, 'html.parser')
        
        # Look for news articles whose title or link contains "preview"
        links = soup.find_all('a', href=re.compile(r'/cricket-news/\d+/'))
        preview_url = None
        for l in links:
            text = l.text.strip().lower()
            href = l['href'].lower()
            if "preview" in text or "preview" in href:
                preview_url = "https://www.cricbuzz.com" + l['href']
                break
                
        if not preview_url:
            # Fallback: search cricbuzz homepage for matching preview news
            home_res = requests.get("https://www.cricbuzz.com/", headers=HEADERS, timeout=15)
            home_soup = BeautifulSoup(home_res.text, 'html.parser')
            home_links = home_soup.find_all('a', href=re.compile(r'/cricket-news/\d+/'))
            
            # Split match title to get team names
            teams = [t.strip().lower() for t in re.split(r'\s+vs\.?\s+', match_title, flags=re.I)]
            if len(teams) >= 2:
                team1_clean = re.split(r',', teams[0])[0].strip()
                team2_clean = re.split(r',', teams[1])[0].strip()
                for hl in home_links:
                    htext = hl.text.strip().lower()
                    if (team1_clean in htext or team2_clean in htext) and "preview" in htext:
                        preview_url = "https://www.cricbuzz.com" + hl['href']
                        break
                        
        if not preview_url:
            return None
            
        art_res = requests.get(preview_url, headers=HEADERS, timeout=15)
        if art_res.status_code != 200:
            return None
            
        art_soup = BeautifulSoup(art_res.text, 'html.parser')
        paras = art_soup.find_all('p')
        
        team_news = []
        stats_trivia = []
        
        for p in paras:
            text = p.text.strip()
            if not text or len(text) < 15:
                continue
                
            # Filter by squad profile links
            profile_links = p.find_all('a', href=re.compile(r'/profiles/(\d+)'))
            skip_para = False
            for pl in profile_links:
                m = re.search(r'/profiles/(\d+)/', pl['href'])
                if m:
                    pid = m.group(1)
                    # If squad is loaded and the player is not in it, skip this paragraph
                    if squad_ids and pid not in squad_ids:
                        skip_para = True
                        break
            if skip_para:
                continue
                
            text_lower = text.lower()
            if any(kw in text_lower for kw in ["injury", "injured", "squad", "miss", "ruled out", "fit", "fitness", "team news", "playing xi", "selection"]):
                team_news.append(text)
            elif any(kw in text_lower for kw in ["needs", "runs to", "wickets to", "did you know", "stat", "record", "history", "milestone", "fifty", "century", "average", "strike rate"]):
                stats_trivia.append(text)
                
        # Format the post
        if not team_news and not stats_trivia:
            # Fallback to general preview description paragraphs if no specific keyword matches
            desc_paras = [p.text.strip() for p in paras if len(p.text.strip()) > 30][:3]
            stats_trivia = desc_paras
            
        post_lines = [
            "🔥 **UPCOMING MATCH PREVIEW & STATS** 🔥",
            "━━━━━━━━━━━━━━━━━━━━",
            f"🏆 **Match:** {add_flags_to_title(match_title)}",
            "📅 **Status:** Preview (Starting soon)",
            ""
        ]
        
        if team_news:
            post_lines.append("🚑 **TEAM & INJURY NEWS:**")
            for tn in team_news[:3]:
                post_lines.append(f"  📌 {tn}")
            post_lines.append("")
            
        if stats_trivia:
            post_lines.append("📈 **STATS & KEY FACTS:**")
            for st in stats_trivia[:4]:
                post_lines.append(f"  ⭐ {st}")
            post_lines.append("")
            
        post_lines.append("━━━━━━━━━━━━━━━━━━━━")
        clean_title = re.sub(r'[^a-zA-Z0-9\s]', '', match_title)
        teams = [t.strip().replace(' ', '') for t in re.split(r'\s+vs\.?\s+', clean_title, flags=re.I)]
        hashtags = "#Cricket #LiveScore #CricketFans"
        if len(teams) >= 2:
            team1_clean = re.split(r',', teams[0])[0].strip()
            team2_clean = re.split(r',', teams[1])[0].strip()
            hashtags = f"#{team1_clean}vs{team2_clean} #CricketStats {hashtags}"
        post_lines.append(hashtags)
        
        return "\n".join(post_lines)
    except Exception as e:
        print(f"[-] Error parsing preview for match {match_id}: {e}")
        return None

def get_match_summary_details(match_id, match_title, match_status):
    """
    Parses completed scorecard from Cricbuzz and formats a top performers summary.
    """
    url = f"https://www.cricbuzz.com/live-cricket-scorecard/{match_id}"
    try:
        res = requests.get(url, headers=HEADERS, timeout=15)
        if res.status_code != 200:
            return None
        soup = BeautifulSoup(res.text, 'html.parser')
        
        # Parse Batsmen
        batsmen_list = []
        bat_grids = soup.find_all('div', class_=lambda c: c and 'scorecard-bat-grid' in c)
        for bg in bat_grids:
            profile_link = bg.find('a', href=re.compile(r'/profiles/\d+/'))
            if not profile_link:
                continue
            name = profile_link.text.strip()
            cols = [c.text.strip() for c in bg.find_all(recursive=False) if c.text.strip()]
            if len(cols) >= 3:
                try:
                    runs = int(cols[1])
                    balls = int(cols[2])
                    batsmen_list.append({
                        "name": name,
                        "runs": runs,
                        "balls": balls
                    })
                except ValueError:
                    continue
                    
        batsmen_list.sort(key=lambda x: (x["runs"], -x["balls"]), reverse=True)
        top_batsmen = batsmen_list[:3]
        
        # Parse Bowlers
        bowlers_list = []
        bowl_grids = soup.find_all('div', class_=lambda c: c and 'scorecard-bowl-grid' in c)
        for bg in bowl_grids:
            profile_link = bg.find('a', href=re.compile(r'/profiles/\d+/'))
            if not profile_link:
                continue
            name = profile_link.text.strip()
            cols = [c.text.strip() for c in bg.find_all(recursive=False) if c.text.strip()]
            if len(cols) >= 5:
                try:
                    overs = cols[1]
                    runs = int(cols[3])
                    wickets = int(cols[4])
                    bowlers_list.append({
                        "name": name,
                        "overs": overs,
                        "runs": runs,
                        "wickets": wickets
                    })
                except ValueError:
                    continue
                    
        bowlers_list.sort(key=lambda x: (x["wickets"], -x["runs"]), reverse=True)
        top_bowlers = bowlers_list[:3]
        
        post_lines = [
            "🏆 **MATCH SUMMARY & RESULT** 🏆",
            "━━━━━━━━━━━━━━━━━━━━",
            f"🏆 **Match:** {add_flags_to_title(match_title)}",
            f"💬 **Status:** {match_status}",
            "",
            "📊 **TOP PERFORMERS:**",
            ""
        ]
        
        if top_batsmen:
            post_lines.append("🏏 **Top Batsmen:**")
            for b in top_batsmen[:2]:
                post_lines.append(f"  👉 {b['name']} - {b['runs']} runs off {b['balls']} balls")
            post_lines.append("")
            
        if top_bowlers:
            post_lines.append("🎳 **Top Bowlers:**")
            for bow in top_bowlers[:2]:
                post_lines.append(f"  👉 {bow['name']} - {bow['wickets']}/{bow['runs']} ({bow['overs']} overs)")
            post_lines.append("")
            
        post_lines.append("━━━━━━━━━━━━━━━━━━━━")
        clean_title = re.sub(r'[^a-zA-Z0-9\s]', '', match_title)
        teams = [t.strip().replace(' ', '') for t in re.split(r'\s+vs\.?\s+', clean_title, flags=re.I)]
        hashtags = "#Cricket #MatchSummary #CricketUpdates #CricketFans"
        if len(teams) >= 2:
            team1_clean = re.split(r',', teams[0])[0].strip()
            team2_clean = re.split(r',', teams[1])[0].strip()
            hashtags = f"#{team1_clean}vs{team2_clean} {hashtags}"
        post_lines.append(hashtags)
        
        return "\n".join(post_lines)
    except Exception as e:
        print(f"[-] Error generating match summary for {match_id}: {e}")
        return None

def check_and_post_milestones(match_id, match_title, batsmen, bowlers, dry_run=False, last_post_cache=None, new_cache=None):
    """
    Parses active batters and bowlers to detect milestones (fifties, centuries, 3-fers, 5-fers).
    Posts alerts to Facebook if new milestones are hit.
    """
    if not last_post_cache:
        last_post_cache = {}
    if not new_cache:
        new_cache = {}
        
    # Check Batsmen Milestones
    for b in batsmen:
        match = re.match(r'([^\-*]+)\*?\s*-\s*(\d+)\s*\((\d+)\)', b)
        if match:
            player_name = match.group(1).strip()
            try:
                runs = int(match.group(2))
                balls = int(match.group(3))
            except ValueError:
                continue
                
            # Century milestone
            if runs >= 100:
                milestone_key = f"milestone_{match_id}_{player_name}_100"
                if last_post_cache.get(milestone_key) != "posted":
                    msg = (
                        f"💯 **CENTURY ALERT!** 💯\n"
                        f"━━━━━━━━━━━━━━━━━━━━\n"
                        f"🏆 **Match:** {add_flags_to_title(match_title)}\n\n"
                        f"🔥 **{player_name}** has just scored a magnificent **CENTURY**! 🌟\n"
                        f"👉 **{runs} runs** off just **{balls} balls**! What an incredible knock! 🏏👏\n\n"
                        f"━━━━━━━━━━━━━━━━━━━━\n"
                        f"#Cricket #Milestone #Century #BattingHero #CricketFans"
                    )
                    print(f"\n--- BATSMAN MILESTONE ALERT (100) ---")
                    print(msg)
                    print("--------------------------------------\n")
                    if dry_run:
                        new_cache[milestone_key] = "posted"
                        new_cache[f"milestone_{match_id}_{player_name}_50"] = "posted"
                    else:
                        success = post_to_facebook(msg)
                        if success:
                            new_cache[milestone_key] = "posted"
                            new_cache[f"milestone_{match_id}_{player_name}_50"] = "posted"
                            save_last_post(new_cache)
                            
            # Fifty milestone
            elif runs >= 50:
                milestone_key = f"milestone_{match_id}_{player_name}_50"
                if last_post_cache.get(milestone_key) != "posted":
                    msg = (
                        f"⭐ **HALF-CENTURY ALERT!** ⭐\n"
                        f"━━━━━━━━━━━━━━━━━━━━\n"
                        f"🏆 **Match:** {add_flags_to_title(match_title)}\n\n"
                        f"🔥 **{player_name}** has reached his **HALF-CENTURY**! 🙌\n"
                        f"👉 **{runs} runs** off **{balls} balls**! A crucial innings for his team! 🏏💪\n\n"
                        f"━━━━━━━━━━━━━━━━━━━━\n"
                        f"#Cricket #Milestone #Fifty #BattingAlert #CricketFans"
                    )
                    print(f"\n--- BATSMAN MILESTONE ALERT (50) ---")
                    print(msg)
                    print("-------------------------------------\n")
                    if dry_run:
                        new_cache[milestone_key] = "posted"
                    else:
                        success = post_to_facebook(msg)
                        if success:
                            new_cache[milestone_key] = "posted"
                            save_last_post(new_cache)
                            
    # Check Bowlers Milestones
    for bowler in bowlers:
        match = re.match(r'([^\-*]+)\s*-\s*(\d+)/(\d+)\s*\(([^)]+)\)', bowler)
        if match:
            player_name = match.group(1).strip()
            try:
                wickets = int(match.group(2))
                runs = int(match.group(3))
                overs = match.group(4).strip()
            except ValueError:
                continue
                
            # 5-Wicket Haul milestone
            if wickets >= 5:
                milestone_key = f"milestone_{match_id}_{player_name}_5"
                if last_post_cache.get(milestone_key) != "posted":
                    msg = (
                        f"🔥 **5-WICKET HAUL ALERT!** 🔥\n"
                        f"━━━━━━━━━━━━━━━━━━━━\n"
                        f"🏆 **Match:** {add_flags_to_title(match_title)}\n\n"
                        f"🎳 **{player_name}** has taken a sensational **5-WICKET HAUL**! 🌟\n"
                        f"👉 Outstanding figures of **{wickets}/{runs}** in just **{overs} overs**! Absolutely dismantled! 🎯🙌\n\n"
                        f"━━━━━━━━━━━━━━━━━━━━\n"
                        f"#Cricket #Milestone #5WicketHaul #BowlingHero #CricketFans"
                    )
                    print(f"\n--- BOWLER MILESTONE ALERT (5) ---")
                    print(msg)
                    print("------------------------------------\n")
                    if dry_run:
                        new_cache[milestone_key] = "posted"
                        new_cache[f"milestone_{match_id}_{player_name}_3"] = "posted"
                    else:
                        success = post_to_facebook(msg)
                        if success:
                            new_cache[milestone_key] = "posted"
                            new_cache[f"milestone_{match_id}_{player_name}_3"] = "posted"
                            save_last_post(new_cache)
                            
            # 3-Wicket milestone
            elif wickets >= 3:
                milestone_key = f"milestone_{match_id}_{player_name}_3"
                if last_post_cache.get(milestone_key) != "posted":
                    msg = (
                        f"🎯 **3-WICKET HAUL!** 🎯\n"
                        f"━━━━━━━━━━━━━━━━━━━━\n"
                        f"🏆 **Match:** {add_flags_to_title(match_title)}\n\n"
                        f"🎳 **{player_name}** is on fire! He picks up **3 wickets**! 💪\n"
                        f"👉 Spell figures: **{wickets}/{runs}** in **{overs} overs**! Keeping the pressure high! 🎳🔥\n\n"
                        f"━━━━━━━━━━━━━━━━━━━━\n"
                        f"#Cricket #Milestone #3Wickets #BowlingAlert #CricketFans"
                    )
                    print(f"\n--- BOWLER MILESTONE ALERT (3) ---")
                    print(msg)
                    print("------------------------------------\n")
                    if dry_run:
                        new_cache[milestone_key] = "posted"
                    else:
                        success = post_to_facebook(msg)
                        if success:
                            new_cache[milestone_key] = "posted"
                            save_last_post(new_cache)

def post_to_facebook(message):
    """
    Posts the message to the Facebook Page.
    """
    if not FB_PAGE_ID or not FB_ACCESS_TOKEN:
        print("[-] Facebook Page ID or Access Token is missing in .env file.")
        return False
        
    url = f"https://graph.facebook.com/v19.0/{FB_PAGE_ID}/feed"
    payload = {
        "message": message,
        "access_token": FB_ACCESS_TOKEN
    }
    
    try:
        response = requests.post(url, data=payload, timeout=15)
        res_json = response.json()
        if response.status_code == 200 and "id" in res_json:
            print(f"[+] Post successful! Post ID: {res_json['id']}")
            return True
        else:
            print(f"[-] Facebook API Error: {res_json}")
            return False
    except Exception as e:
        print(f"[-] Exception posting to Facebook: {e}")
        return False

def load_last_post():
    """Loads the last posted score info to avoid repeating posts."""
    if os.path.exists(LAST_POST_FILE):
        try:
            with open(LAST_POST_FILE, 'r') as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def save_last_post(data):
    """Saves the last posted score info."""
    try:
        with open(LAST_POST_FILE, 'w') as f:
            json.dump(data, f)
    except Exception as e:
        print(f"[-] Error saving last post state: {e}")

def run_bot(dry_run=False):
    """
    Core function that fetches live matches, scrapes details, and posts to Facebook.
    """
    print("[*] Checking Cricbuzz for live matches...")
    matches = get_live_match_links()
    
    if not matches:
        print("[-] No live matches found on Cricbuzz.")
        return
        
    print(f"[+] Found {len(matches)} matches. Scraping details...")
    
    # Load last posted score cache
    last_post_cache = load_last_post()
    new_cache = dict(last_post_cache)
    
    for match_info in matches:
        match_id = match_info["id"]
        
        # Scrape details
        info = scrape_match_details(match_info)
        if not info:
            continue
        
        status_lower = info["status"].lower()
        
        # Check if it is a preview-state match
        is_preview = "preview" in status_lower
        if is_preview:
            if is_international_match(info["title"]):
                preview_key = f"preview_{match_id}"
                if last_post_cache.get(preview_key) != "posted":
                    print(f"[*] Fetching preview and stats alert for {info['title']}...")
                    preview_msg = get_match_preview_alert(match_id, info["title"])
                    if preview_msg:
                        print("\n--- PREVIEW FACEBOOK POST ---")
                        print(preview_msg)
                        print("-----------------------------\n")
                        
                        if dry_run:
                            print("[*] Dry run mode active. Skipping Facebook post.")
                            new_cache[preview_key] = "posted"
                        else:
                            success = post_to_facebook(preview_msg)
                            if success:
                                new_cache[preview_key] = "posted"
                                save_last_post(new_cache)
                    else:
                        print(f"[-] No preview details found for {info['title']}.")
            else:
                print(f"[*] Match {info['title']} is a league/non-international preview. Skipping.")
            continue
            
        # Check if it is a completed match to publish summary
        is_completed = any(kw in status_lower for kw in ['won by', 'won', 'beat', 'complete', 'completed', 'abandoned', 'no result', 'tie', 'stumps'])
        if is_completed:
            summary_key = f"summary_{match_id}"
            if last_post_cache.get(summary_key) != "posted":
                print(f"[*] Match completed: {info['title']}. Fetching summary...")
                summary_msg = get_match_summary_details(match_id, info["title"], info["status"])
                if summary_msg:
                    print("\n--- SUMMARY FACEBOOK POST ---")
                    print(summary_msg)
                    print("-----------------------------\n")
                    if dry_run:
                        new_cache[summary_key] = "posted"
                    else:
                        success = post_to_facebook(summary_msg)
                        if success:
                            new_cache[summary_key] = "posted"
                            save_last_post(new_cache)
            continue
            
        # Only post matches that are currently active/live
        is_live = False
        
        # Live indicators
        if any(kw in status_lower for kw in ['live', 'need', 'trail by', 'lead by', 'opt to', 'delayed', 'rain']):
            is_live = True
        elif info["scores"] and not any(kw in status_lower for kw in ['won by', 'won', 'beat', 'complete', 'completed', 'stumps', 'abandoned', 'no result']):
            is_live = True
            
        if not is_live:
            print(f"[*] Match {info['title']}: Match is not currently live (Status: {info['status']}). Skipping.")
            continue
            
        # We define uniqueness by scores + status + active batters combined
        batsmen_str = "|".join(info.get("batsmen", []))
        score_state_str = "|".join(info["scores"]) + f"|{info['status']}|{batsmen_str}"
        
        # Save to new cache
        new_cache[match_id] = score_state_str
        
        # Check if this match's score is updated compared to the last post
        last_state = last_post_cache.get(match_id)
        
        if last_state != score_state_str:
            # Format the post
            formatted_message = format_score_post(info)
            
            print("\n--- PROFESSIONAL FACEBOOK POST ---")
            print(formatted_message)
            print("----------------------------------\n")
            
            if dry_run:
                print("[*] Dry run mode active. Skipping Facebook post.")
            else:
                # Post to Facebook
                success = post_to_facebook(formatted_message)
                if not success:
                    # If post failed, do not update cache so we try again next time
                    new_cache[match_id] = last_state
                    
        # Check and post milestones (runs/wickets)
        check_and_post_milestones(
            match_id=match_id,
            match_title=info["title"],
            batsmen=info.get("batsmen", []),
            bowlers=info.get("bowlers", []),
            dry_run=dry_run,
            last_post_cache=last_post_cache,
            new_cache=new_cache
        )
                
    # Update cache file
    if not dry_run:
        save_last_post(new_cache)

def run_health_check_server():
    """
    Starts a simple HTTP server to satisfy cloud providers' port binding health checks (e.g. Railway, Render).
    """
    port = int(os.getenv("PORT", 8080))
    server_address = ('', port)
    
    class HealthCheckHandler(SimpleHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header('Content-type', 'text/plain')
            self.end_headers()
            self.wfile.write(b"OK")
            
        def log_message(self, format, *args):
            # Suppress default server logs to keep stdout clean
            return

    try:
        httpd = HTTPServer(server_address, HealthCheckHandler)
        print(f"[*] Started health check server on port {port}")
        httpd.serve_forever()
    except Exception as e:
        print(f"[-] Error starting health check server: {e}")

def main():
    dry_run = "--dry-run" in sys.argv
    once = "--once" in sys.argv or os.getenv("RUN_ONCE") == "true"
    
    if dry_run:
        print("[*] Starting Bot in Dry Run mode (Console Output Only)...")
        run_bot(dry_run=True)
        return
        
    print("[*] Starting Cricbuzz Facebook Score Bot...")
    
    if once:
        print("[*] Running in single-run mode...")
        run_bot(dry_run=False)
        return
        
    print("[*] Bot will check and post scores every 15 minutes.")
    
    # Start health check server for Railway/Render port binding in a background thread
    t = threading.Thread(target=run_health_check_server, daemon=True)
    t.start()
    
    # Run immediately on startup
    run_bot(dry_run=False)
    
    # Continuous loop
    try:
        while True:
            # Wait for 15 minutes (900 seconds)
            time.sleep(900)
            run_bot(dry_run=False)
    except KeyboardInterrupt:
        print("[*] Bot stopped by user.")

if __name__ == "__main__":
    main()
