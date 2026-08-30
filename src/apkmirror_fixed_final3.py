import re
import json
import logging
from bs4 import BeautifulSoup
from urllib.parse import quote
from src import session

base_url = "https://www.apkmirror.com"
_blocked_by_cloudflare = False


class ApkMirrorBlocked(RuntimeError):
    """APKMirror declined this runner before it served an application page."""


def _app_slug_candidates(config: dict) -> list[str]:
    """Return the small set of valid-looking APKMirror app slugs to try.

    On APKMirror the publisher slug and app slug are sometimes identical
    (for example ``/apk/pinterest/pinterest/``), while a human-readable app
    title can be much longer.  Trying the publisher as a final fallback fixes
    those genuine 404s without a site-wide search or browser automation.
    """
    candidates = [
        config.get("app_slug"),
        config.get("name"),
        config.get("org"),
    ]
    return list(dict.fromkeys(slug for slug in candidates if slug))


def _cf_get(url, **kwargs):
    """Fetch without trying to defeat Cloudflare on a GitHub-hosted runner."""
    global _blocked_by_cloudflare
    if _blocked_by_cloudflare:
        raise ApkMirrorBlocked("APKMirror blocked this runner earlier in the build")

    kwargs.setdefault("timeout", 20)
    response = session.get(url, **kwargs)
    if response.status_code == 403:
        body = response.text[:2000].lower()
        if response.headers.get("cf-mitigated") == "challenge" or "cloudflare" in body:
            _blocked_by_cloudflare = True
            logging.warning(
                "APKMirror served a Cloudflare challenge; skipping APKMirror "
                "for this build instead of launching a browser."
            )
            raise ApkMirrorBlocked("APKMirror Cloudflare challenge")
    return response

def get_build_number_for_version(version: str, config: dict) -> tuple[str | None, str]:
    """Fetch build number for a specific version from APKMirror.
    Returns (build_number, format_type) where format_type is 'parentheses' or 'build_suffix'.
    Returns the LOWEST build number found, since patches are typically made for initial builds."""
    try:
        main_url = f"{base_url}/apk/{config['org']}/{config['name']}/"
        response = _cf_get(main_url)
        if response.status_code == 200:
            soup = BeautifulSoup(response.content, "html.parser")
            # Collect all build numbers for this version
            builds_found = []
            for link in soup.find_all('a', href=True):
                text = link.get_text()
                if version in text:
                    # Format 1: "32.30.0(1575420)" -> parentheses
                    build_match = re.search(rf'{re.escape(version)}\((\d+)\)', text)
                    if build_match:
                        builds_found.append((build_match.group(1), 'parentheses'))
                    # Format 2: "6.6 build 006" -> build suffix
                    build_match = re.search(rf'{re.escape(version)}\s+build\s+(\d+)', text, re.IGNORECASE)
                    if build_match:
                        builds_found.append((build_match.group(1), 'build_suffix'))
            
            # Return the lowest build number (patches are typically for initial builds)
            if builds_found:
                # Sort by build number (as integer) and return the lowest
                builds_found.sort(key=lambda x: int(x[0]))
                return builds_found[0]
    except Exception as e:
        logging.debug(f"Could not fetch build number: {e}")
    return None, None

def discover_app_main_url(config: dict) -> str | None:
    """Use APKMirror's search endpoint to discover the correct main app page URL when
    the configured 'org/name' combination doesn't match APKMirror's actual URL slugs.
    
    For example, config has org='duolingo', name='duolingo' but the actual page is at
    /apk/duolingo/duolingo-duolingo/. This function searches APKMirror and finds the
    correct main page URL by matching the org and the package name (most reliable).
    
    Returns the full main page URL if found, or None if discovery fails."""
    try:
        org = config.get('org', '')
        name = config.get('name', '')
        package = config.get('package', '')
        
        # Build search query - use package name if available (most precise), else app name
        # Strip ".apk" or trailing dashes from name for cleaner search
        query_terms = []
        if package:
            query_terms.append(package)
        if name:
            query_terms.append(name.replace('-', ' '))
        
        for query in query_terms:
            search_url = f"{base_url}/?post_type=app_release&searchtype=app&s={quote(query)}"
            logging.info(f"Searching APKMirror for app: {search_url}")
            
            try:
                response = _cf_get(search_url)
                if response.status_code != 200:
                    continue
                
                soup = BeautifulSoup(response.content, "html.parser")
                
                # Find all /apk/{org}/{slug}/ links - these are candidate main app pages
                # We prioritize matches under the same 'org' as the config
                found_links = set()
                for link in soup.find_all('a', href=True):
                    href = link['href']
                    # Match pattern /apk/{org}/{slug}/ but NOT /apk/{org}/{slug}/{anything-else}
                    m = re.match(r'^(/apk/[a-z0-9._-]+/[a-z0-9._-]+/)$', href)
                    if m:
                        found_links.add(m.group(1))
                
                if not found_links:
                    continue
                
                # Prefer links under the configured org
                org_links = [link for link in found_links if link.startswith(f"/apk/{org}/")]
                
                # Among org-matching links, find the one most likely to be the right app
                # Strategy: pick one whose slug contains the configured name as a substring
                # If multiple, prefer the shorter slug (more "exact" match)
                candidates = org_links if org_links else list(found_links)
                
                # Filter candidates: prefer those containing 'name' in the slug
                name_matches = [link for link in candidates if name and name in link]
                if name_matches:
                    candidates = name_matches
                
                # Sort by slug length (shorter = more specific match)
                candidates.sort(key=lambda x: len(x))
                
                if candidates:
                    discovered = base_url + candidates[0]
                    logging.info(f"✓ Discovered main app page via search: {discovered}")
                    return discovered
            except Exception as e:
                logging.debug(f"Error during search query '{query}': {e}")
                continue
        
        logging.debug("No matching app found via search")
        return None
        
    except Exception as e:
        logging.debug(f"Error in discover_app_main_url: {e}")
        return None

def _scrape_release_url_from_soup(soup, version: str, config: dict, build_number: str = None, build_format: str = None) -> str | None:
    """Scan a BeautifulSoup-parsed main app page for a release link matching the version.
    Returns the full release page URL if found, else None."""
    version_parts = version.split('.')
    
    # Try full version first, then progressively strip parts (e.g., 6.77.5 -> 6.77 -> 6)
    for i in range(len(version_parts), 0, -1):
        current_ver = ".".join(version_parts[:i])
        current_ver_dash = "-".join(version_parts[:i])
        
        # Build search patterns for matching
        search_patterns = [current_ver, current_ver_dash]
        if build_number and i == len(version_parts):
            if build_format == 'build_suffix':
                search_patterns.append(f"{current_ver} build {build_number}")
            else:
                search_patterns.append(f"{current_ver}({build_number})")
        
        # Find candidate release links (those containing the dashed version)
        # APKMirror release URLs look like: /apk/{org}/{app-slug}/{release-slug}-{version}-release/
        candidates = []
        for link in soup.find_all('a', href=True):
            href = link['href']
            if not href.startswith('/apk/'):
                continue
            # Must look like a release page: contain the dashed version
            # Use regex to check the version is properly bounded (not part of a longer number)
            # e.g., for "6-77-5", match -6-77-5- or -6-77-5/
            ver_pattern = re.escape(current_ver_dash)
            if re.search(rf'(?:^|[/-]){ver_pattern}(?:[/-]|$)', href):
                # Prefer URLs ending with -release/
                priority = 0 if href.rstrip('/').endswith('-release') else 1
                candidates.append((priority, href))
        
        if candidates:
            # Sort by priority (release pages first), then by length (shorter = more specific)
            candidates.sort(key=lambda x: (x[0], len(x[1])))
            chosen = candidates[0][1]
            full_url = base_url + chosen
            logging.info(f"✓ Found release page on main listing for {current_ver}: {full_url}")
            return full_url
    
    return None

def find_release_page_from_main(version: str, config: dict, build_number: str = None, build_format: str = None) -> str | None:
    """Scrape the main app listing page on APKMirror to find the correct release page URL
    for a specific version. This avoids URL construction from config fields, which may not
    match APKMirror's actual URL slugs (e.g., 'duolingo' vs 'duolingo-language-lessons').
    
    Strategy:
    1. Try the configured main page (org/name from config)
    2. If that 404s, use APKMirror search to discover the correct main page URL
    3. Scrape release links from whichever main page works
    
    Returns the full release page URL if found, or None if scraping fails."""
    try:
        # Step 1: Try configured main page first (works for most apps)
        main_url = f"{base_url}/apk/{config['org']}/{config['name']}/"
        response = _cf_get(main_url)
        
        soup = None
        if response.status_code == 200:
            soup = BeautifulSoup(response.content, "html.parser")
            result = _scrape_release_url_from_soup(soup, version, config, build_number, build_format)
            if result:
                return result
            logging.debug(f"Main page accessible but no version match: {main_url}")
        else:
            logging.info(f"Configured main page returned {response.status_code}: {main_url}")
        
        # Step 2: If configured main page failed or didn't yield a match, try discovering
        # the correct main page via APKMirror's search endpoint
        discovered_url = discover_app_main_url(config)
        if discovered_url and discovered_url != main_url:
            logging.info(f"Trying discovered main page: {discovered_url}")
            response = _cf_get(discovered_url)
            if response.status_code == 200:
                soup = BeautifulSoup(response.content, "html.parser")
                result = _scrape_release_url_from_soup(soup, version, config, build_number, build_format)
                if result:
                    return result
        
        logging.debug(f"Could not find release page URL from main listing for version {version}")
        return None
        
    except Exception as e:
        logging.debug(f"Error scraping main page for release URL: {e}")
        return None

def get_download_link(version: str, app_name: str, config: dict, arch: str = None) -> str:
    if not version:
        logging.error(f"No version provided for {app_name}")
        return None
        
    target_arch = arch if (arch and arch != "universal") else config.get('arch', 'universal')
    
    criteria = [config['type'], target_arch, config['dpi']]
    
    # --- UNIVERSAL URL FINDER WITH VALIDATION ---
    # Extract build number if present (e.g., "32.30.0(1575420)" -> version="32.30.0", build="1575420")
    build_number = None
    build_format = None
    
    # Check for parentheses format: "32.30.0(1575420)"
    build_match = re.search(r'\((\d+)\)$', version)
    if build_match:
        build_number = build_match.group(1)
        build_format = 'parentheses'
        version = version[:build_match.start()]
    else:
        # Check for build suffix format: "6.6 build 002"
        build_match = re.search(r'\s+build\s+(\d+)$', version, re.IGNORECASE)
        if build_match:
            build_number = build_match.group(1)
            build_format = 'build_suffix'
            version = version[:build_match.start()]
        else:
            # Try to fetch build number from APKMirror for this version
            build_number, build_format = get_build_number_for_version(version, config)
            if build_number:
                logging.info(f"Found build number {build_number} for version {version} (format: {build_format})")
    
    version_parts = version.split('.')
    found_soup = None
    correct_version_page = False
    
    # --- PRIMARY APPROACH: Scrape the main app page for the correct release URL ---
    # This is more reliable than constructing URLs from config fields, because
    # APKMirror's actual URL slugs often differ from config values
    # (e.g., 'duolingo' slug vs 'duolingo-language-lessons' actual release name)
    scraped_url = find_release_page_from_main(version, config, build_number, build_format)
    if scraped_url:
        logging.info(f"Trying scraped release URL: {scraped_url}")
        try:
            response = _cf_get(scraped_url)
            if response.status_code == 200:
                soup = BeautifulSoup(response.content, "html.parser")
                page_text = soup.get_text()
                # Quick validation: check version appears on page
                if version in page_text or version.replace('.', '-') in page_text:
                    logging.info(f"✓ Scraped release page validated: {response.url}")
                    found_soup = soup
                    correct_version_page = True
                else:
                    logging.warning(f"Scraped URL returned page but version {version} not found in content")
        except Exception as e:
            logging.warning(f"Error fetching scraped URL: {e}")

    # Once Cloudflare has challenged this runner, generated URL probes cannot
    # succeed. Stop here so one app does not emit misleading 404s for every
    # possible release slug and the configured fallback can run immediately.
    if _blocked_by_cloudflare:
        return None
    
    # --- FALLBACK: Construct URLs from config fields ---
    # Only used if scraping the main page didn't work
    if not correct_version_page:
        logging.info("Scraping didn't find the page, falling back to URL construction...")
    
    # Use release_prefix if available, otherwise use app name
    release_name = config.get('release_prefix', config['name'])
    app_slugs = _app_slug_candidates(config)
    
    # Loop backwards: Try full version, then strip parts
    for i in range(len(version_parts), 0, -1):
        current_ver_str = "-".join(version_parts[:i])
        
        # If build number exists, append it to the last version part in URL
        if build_number and i == len(version_parts):
            if build_format == 'build_suffix':
                # e.g., "6-6" + "build-006" -> "6-6-build-006"
                current_ver_str = current_ver_str + "-build-" + build_number
            else:
                # e.g., "32-30-0" + "1575420" -> "32-30-01575420"
                parts = version_parts[:i]
                parts[-1] = parts[-1] + build_number
                current_ver_str = "-".join(parts)
        
        # Generate ALL possible URL patterns in priority order
        url_patterns = []
        
        # URL-encode the release_name to handle unicode characters like ․
        encoded_release_name = quote(release_name, safe='')
        org = config.get('org', '')
        encoded_org = quote(org, safe='')

        for app_slug in app_slugs:
            encoded_name = quote(app_slug, safe='')

            # Prefer the explicit release slug; it is more stable than a
            # display name and supports apps whose title changes over time.
            url_patterns.append(f"{base_url}/apk/{org}/{encoded_name}/{encoded_release_name}-{current_ver_str}-release/")

            if release_name != app_slug:
                url_patterns.append(f"{base_url}/apk/{org}/{encoded_name}/{encoded_name}-{current_ver_str}-release/")

            if org and org != release_name and org != app_slug:
                url_patterns.append(f"{base_url}/apk/{org}/{encoded_name}/{encoded_org}-{current_ver_str}-release/")

            url_patterns.append(f"{base_url}/apk/{org}/{encoded_name}/{encoded_release_name}-{current_ver_str}/")

            if release_name != app_slug:
                url_patterns.append(f"{base_url}/apk/{org}/{encoded_name}/{encoded_name}-{current_ver_str}/")

            if org and org != release_name and org != app_slug:
                url_patterns.append(f"{base_url}/apk/{org}/{encoded_name}/{encoded_org}-{current_ver_str}/")
        
        # Remove duplicate patterns
        url_patterns = list(dict.fromkeys(url_patterns))
        
        for url in url_patterns:
            logging.info(f"Checking potential release URL: {url}")
            
            try:
                response = _cf_get(url)
                if response.status_code == 200:
                    soup = BeautifulSoup(response.content, "html.parser")
                    page_text = soup.get_text()
                    
                    # VALIDATION: Check if this page is for our EXACT version
                    # Check multiple possible version formats
                    version_checks = [
                        version,  # 6.6
                        version.replace('.', '-'),  # 6-6
                        current_ver_str,  # 6-6-build-002 (if stripped)
                        ".".join(version_parts[:i])  # 6.6 (if stripped)
                    ]
                    
                    # Add build suffix format if we have a build number
                    if build_number:
                        if build_format == 'build_suffix':
                            version_checks.append(f"{version} build {build_number}")  # 6.6 build 002
                            version_checks.append(f"{version.replace('.', '-')}-build-{build_number}")  # 6-6-build-002
                        else:
                            version_checks.append(f"{version}({build_number})")  # 32.30.0(1575420)
                    
                    # Also check page title and headings for version
                    title_tag = soup.find('title')
                    headings = soup.find_all(['h1', 'h2', 'h3'])
                    
                    is_correct_page = False
                    
                    # Check in page text
                    for check in version_checks:
                        if check and check in page_text:
                            # Accept version match if it's the base version or includes build info
                            if check == version or check == version.replace('.', '-') or check == current_ver_str:
                                is_correct_page = True
                                break
                    
                    # Check in title and headings
                    if not is_correct_page:
                        for heading in headings:
                            heading_text = heading.get_text()
                            for check in version_checks:
                                if check and check in heading_text:
                                    is_correct_page = True
                                    break
                            if is_correct_page:
                                break
                    
                    if not is_correct_page and title_tag:
                        title_text = title_tag.get_text()
                        for check in version_checks:
                            if check and check in title_text:
                                is_correct_page = True
                                break
                    
                    if is_correct_page:
                        content_size = len(response.content)
                        logging.info(f"✓ Correct version page found: {response.url}")
                        found_soup = soup
                        correct_version_page = True
                        break  # Found correct page!
                    else:
                        # Page exists but doesn't have our version as primary
                        logging.warning(f"Page found but not for version {version}: {url}")
                        # Save as fallback ONLY if we haven't found any page yet
                        if found_soup is None:
                            found_soup = soup
                            logging.warning(f"Saved as fallback page (may list multiple versions)")
                        continue
                        
                elif response.status_code == 404:
                    logging.info(f"URL not found (404): {url}")
                    continue
                else:
                    logging.warning(f"URL {url} returned status {response.status_code}")
                    continue
                    
            except Exception as e:
                logging.warning(f"Error checking {url}: {str(e)[:50]}")
                continue
        
        if correct_version_page:
            break  # Found correct page for this version part
    
    # If we didn't find the exact version page but found a fallback
    if not correct_version_page and found_soup:
        logging.warning(f"Using fallback page for {app_name} {version} (may contain multiple versions)")
    
    if not found_soup:
        logging.error(f"Could not find any release page for {app_name} {version}")
        return None
    
    # --- DEBUG: SAVE APKMIRROR HTML ---
    try:
        with open("apkmirror_debug.html", "w", encoding="utf-8") as debug_file:
            debug_file.write(str(found_soup))
        logging.info(
            f"DEBUG: saved APKMirror HTML ({len(str(found_soup))} bytes)"
        )
    except Exception as debug_error:
        logging.warning(
            f"DEBUG: could not save APKMirror HTML: {debug_error}"
        )

    # --- VARIANT FINDER ---

    def _clean_text(value):
        return re.sub(r'\s+', ' ', value or '').strip()

    def _extract_android_version(text):
        text = _clean_text(text).lower()
        match = re.search(r'android\s+(\d+(?:\.\d+)?)', text, re.IGNORECASE)
        if match:
            try:
                return float(match.group(1))
            except ValueError:
                pass

        match = re.search(r'(?:api[-\s]?)(\d+)', text, re.IGNORECASE)
        if match:
            api = int(match.group(1))
            api_map = {
                35: 15, 34: 14, 33: 13, 32: 12, 31: 12,
                30: 11, 29: 10, 28: 9, 27: 8.1, 26: 8,
                25: 7.1, 24: 7, 23: 6, 22: 5.1, 21: 5,
            }
            return api_map.get(api)
        return None

    def _extract_file_size(text):
        text = _clean_text(text).lower()
        matches = re.findall(r'(\d+(?:\.\d+)?)\s*(gb|mb|kb|b)\b', text, re.IGNORECASE)
        if not matches:
            return float('inf')

        sizes = []
        for value, unit in matches:
            value = float(value)
            if unit == 'gb':
                value *= 1024 ** 3
            elif unit == 'mb':
                value *= 1024 ** 2
            elif unit == 'kb':
                value *= 1024
            sizes.append(value)
        return min(sizes)

    def _extract_variant_link(row):
        links = row.find_all('a', href=True)
        candidates = []

        for link in links:
            href = link.get('href', '').strip()
            if not href:
                continue

            text = _clean_text(link.get_text(" ", strip=True)).lower()
            classes = " ".join(link.get('class', [])).lower()
            score = 0

            if '/apk/' in href:
                score += 10
            if 'download' in text:
                score += 5
            if 'download' in classes:
                score += 5
            if 'accent_color' in classes:
                score += 3

            candidates.append((score, link))

        if not candidates:
            return None

        candidates.sort(key=lambda item: item[0], reverse=True)
        return candidates[0][1].get('href')

    def _extract_variant(row, index):
        text = _clean_text(row.get_text(" ", strip=True))
        if not text:
            return None

        lower = text.lower()

        is_apk = bool(re.search(r'\bapk\b', lower))
        is_bundle = bool(re.search(r'\b(?:bundle|apk\s+bundle|aab)\b', lower))

        if not (is_apk or is_bundle):
            return None

        architectures = set(
            re.findall(
                r'\b(?:arm64-v8a|armeabi-v7a|x86_64|x86|universal)\b',
                lower
            )
        )

        dpi_ranges = []
        for low, high in re.findall(r'(\d+)\s*-\s*(\d+)\s*dpi', lower):
            dpi_ranges.append((int(low), int(high)))

        dpis = []
        for match in re.finditer(r'(?<!-)(\d+)\s*dpi\b', lower):
            dpi = int(match.group(1))
            before = lower[max(0, match.start() - 12):match.start()]
            if re.search(r'\d+\s*-\s*$', before):
                continue
            dpis.append(dpi)

        dpis = sorted(set(dpis), reverse=True)

        is_nodpi = bool(re.search(r'\bnodpi\b', lower))
        android_version = _extract_android_version(text)
        file_size = _extract_file_size(text)
        href = _extract_variant_link(row)

        if not href:
            return None

        return {
            "index": index,
            "text": text,
            "lower": lower,
            "is_apk": is_apk,
            "is_bundle": is_bundle,
            "architectures": architectures,
            "dpi_ranges": dpi_ranges,
            "dpis": dpis,
            "is_nodpi": is_nodpi,
            "android_version": android_version,
            "file_size": file_size,
            "href": href,
        }

    # ------------------------------------------------------------------
    # APKMirror variant extraction
    #
    # IMPORTANT:
    # The release page also contains "ALL VARIANTS" / historical entries.
    # Never treat those as variants of the requested release.  The only
    # reliable source here is the variant-{JSON} URL itself.
    # ------------------------------------------------------------------

    def _variant_from_href(href, index, link_text=""):
        if not href:
            return None

        full_href = href
        if full_href.startswith("/"):
            full_href = base_url + full_href

        m = re.search(r'/variant-(%7B.*%7D|%7b.*%7d)/?$', full_href, re.I)
        if not m:
            return None

        encoded = m.group(1)
        try:
            from urllib.parse import unquote
            payload = json.loads(unquote(encoded))
        except Exception:
            return None

        arches = payload.get("arches_slug") or []
        dpis_slug = payload.get("dpis_slug") or []
        minapi_slug = payload.get("minapi_slug")

        architectures = set()
        for arch in arches:
            a = str(arch).lower()
            if a == "arm64-v8a":
                architectures.add("arm64-v8a")
            elif a == "armeabi-v7a":
                architectures.add("armeabi-v7a")
            elif a in ("x86_64", "x86-64"):
                architectures.add("x86_64")
            elif a == "x86":
                architectures.add("x86")
            elif a == "universal":
                architectures.add("universal")
            elif a == "noarch":
                architectures.add("noarch")
            elif a == "armeabi":
                architectures.add("armeabi")

        dpi_ranges = []
        dpis = []
        is_nodpi = False

        for dpi in dpis_slug:
            s = str(dpi).lower().strip()
            if s == "nodpi":
                is_nodpi = True
                continue

            # APKMirror slugs may be "dpi-120-640", "120-640", "120dpi", etc.
            nums = [int(x) for x in re.findall(r"\d+", s)]
            if len(nums) >= 2:
                dpi_ranges.append((nums[-2], nums[-1]))
            elif len(nums) == 1:
                dpis.append(nums[0])

        android_version = None
        if minapi_slug:
            nums = re.findall(r"\d+", str(minapi_slug))
            if nums:
                api = int(nums[-1])
                # Common Android API mappings needed for tie-breaking.
                api_to_android = {
                    35: 15, 34: 14, 33: 13, 32: 12,
                    31: 12, 30: 11, 29: 10, 28: 9,
                    27: 8.1, 26: 8, 25: 7.1, 24: 7,
                    23: 6, 22: 5.1, 21: 5, 19: 4.4,
                    18: 4.3, 17: 4.2, 16: 4.1, 15: 4,
                    14: 4, 13: 3.2, 12: 3.1, 11: 3,
                    10: 2.3, 9: 2.3, 8: 2.2, 7: 2.1,
                }
                android_version = api_to_android.get(api, api)

        # The variant page itself determines whether it is APK/BUNDLE.
        # Start unknown; the download page parser can resolve it later.
        lower_text = _clean_text(link_text).lower()
        is_bundle = bool(re.search(r"\bbundle\b|\baab\b", lower_text))
        is_apk = bool(re.search(r"\bapk\b", lower_text)) and not is_bundle

        return {
            "index": index,
            "text": _clean_text(link_text),
            "lower": lower_text,
            "is_apk": is_apk,
            "is_bundle": is_bundle,
            "architectures": architectures,
            "dpi_ranges": dpi_ranges,
            "dpis": sorted(set(dpis), reverse=True),
            "is_nodpi": is_nodpi,
            "android_version": android_version,
            "file_size": float("inf"),
            "href": full_href,
            "variant_payload": payload,
        }

    # Collect ONLY variant-{JSON} links that are actually present on the
    # current release page. Do not walk upward to arbitrary page containers.
    variants = []
    seen_variant_urls = set()

    for link in found_soup.find_all("a", href=True):
        href = link.get("href", "").strip()
        if "/variant-" not in href.lower():
            continue

        variant = _variant_from_href(
            href,
            len(variants),
            link.get_text(" ", strip=True)
        )
        if not variant:
            continue

        if variant["href"] in seen_variant_urls:
            continue

        seen_variant_urls.add(variant["href"])
        variants.append(variant)

    # Some APKMirror markup stores the variant URL in data attributes.
    if not variants:
        for element in found_soup.find_all(True):
            for attr in ("href", "data-href", "data-url", "data-variant-url"):
                href = element.get(attr)
                if not href or "/variant-" not in str(href).lower():
                    continue

                variant = _variant_from_href(
                    str(href),
                    len(variants),
                    element.get_text(" ", strip=True)
                )
                if not variant:
                    continue

                if variant["href"] in seen_variant_urls:
                    continue

                seen_variant_urls.add(variant["href"])
                variants.append(variant)

    logging.info(f"Detected {len(variants)} APKMirror variants for {app_name} {version}")

    for variant in variants[:20]:
        logging.info(
            "Variant: "
            f"arch={sorted(variant['architectures'])}, "
            f"dpi_ranges={variant['dpi_ranges']}, "
            f"dpis={variant['dpis']}, "
            f"nodpi={variant['is_nodpi']}, "
            f"min_android={variant['android_version']}, "
            f"href={variant['href']}"
        )

    def _range_width(r):
        return max(0, r[1] - r[0])

    def _dpi_options(variant):
        opts = []
        for low, high in variant["dpi_ranges"]:
            opts.append(("range", low, high, _range_width((low, high))))
        for dpi in variant["dpis"]:
            opts.append(("single", dpi, dpi, 0))
        return opts

    TARGET_DPI = 522

    def _contains_target(variant):
        return any(low <= TARGET_DPI <= high
                   for _, low, high, _ in _dpi_options(variant))

    def _higher_than_target(variant):
        return any(low > TARGET_DPI or high > TARGET_DPI
                   for _, low, high, _ in _dpi_options(variant))

    def _closest_distance(variant):
        distances = []
        for _, low, high, _ in _dpi_options(variant):
            if low <= TARGET_DPI <= high:
                distances.append(0)
            elif TARGET_DPI < low:
                distances.append(low - TARGET_DPI)
            else:
                distances.append(TARGET_DPI - high)
        return min(distances) if distances else float("inf")

    def _tie_break(items):
        if not items:
            return None

        # Exact tie rules:
        # Minimum Android 12 first, then lower minimum, then smaller size,
        # then original order.
        def key(v):
            av = v["android_version"]
            if av is None:
                android_key = 1
                distance_from_12 = float("inf")
            else:
                android_key = 0 if av == 12 else 1
                distance_from_12 = abs(av - 12)

            return (
                android_key,
                distance_from_12,
                v["file_size"],
                v["index"],
            )

        return min(items, key=key)

    def _select_for_architecture(architecture):
        candidates = [
            v for v in variants
            if architecture in v["architectures"]
        ]

        if not candidates:
            return None

        # 1) Narrowest range/singleton containing 522.
        containing = []
        for v in candidates:
            options = [
                o for o in _dpi_options(v)
                if o[1] <= TARGET_DPI <= o[2]
            ]
            if options:
                width = min(o[3] for o in options)
                containing.append((width, v))

        if containing:
            min_width = min(width for width, _ in containing)
            return _tie_break(
                [v for width, v in containing if width == min_width]
            )

        # 2) If nothing contains 522, consider every option having a number
        # above 522, regardless of range vs single DPI, and choose smallest
        # file size. If sizes tie, use the normal tie-break rules.
        higher = [v for v in candidates if _higher_than_target(v)]
        if higher:
            min_size = min(v["file_size"] for v in higher)
            return _tie_break(
                [v for v in higher if v["file_size"] == min_size]
            )

        # 3) nodpi.
        nodpi = [v for v in candidates if v["is_nodpi"]]
        if nodpi:
            return _tie_break(nodpi)

        # 4) Closest DPI to 522, then smallest size.
        distances = [( _closest_distance(v), v) for v in candidates]
        min_distance = min(d for d, _ in distances)
        closest = [v for d, v in distances if d == min_distance]
        min_size = min(v["file_size"] for v in closest)
        return _tie_break([v for v in closest if v["file_size"] == min_size])

    # Architecture priority: arm64-v8a -> universal -> noarch/nodpi.
    selected_variant = _select_for_architecture("arm64-v8a")
    selected_arch = "arm64-v8a" if selected_variant else None

    if not selected_variant:
        selected_variant = _select_for_architecture("universal")
        if selected_variant:
            selected_arch = "universal"

    if not selected_variant:
        selected_variant = _select_for_architecture("noarch")
        if selected_variant:
            selected_arch = "noarch"

    if not selected_variant:
        logging.error(
            f"No APK/BUNDLE variant found for {app_name} {version}"
        )
        return None

    href = selected_variant["href"]
    download_page_url = href if href.startswith("http") else base_url + href

    selected_type = (
        "BUNDLE" if selected_variant["is_bundle"] else "APK"
    )

    if selected_variant["is_nodpi"]:
        selected_dpi = "nodpi"
    elif selected_variant["dpi_ranges"]:
        # The selected variant may have more than one range; report the
        # narrowest range containing 522, otherwise the narrowest range.
        containing = [
            r for r in selected_variant["dpi_ranges"]
            if r[0] <= TARGET_DPI <= r[1]
        ]
        chosen_range = min(
            containing or selected_variant["dpi_ranges"],
            key=lambda r: (r[1] - r[0], r[0])
        )
        selected_dpi = f"{chosen_range[0]}-{chosen_range[1]}dpi"
    elif selected_variant["dpis"]:
        selected_dpi = f"{selected_variant['dpis'][0]}dpi"
    else:
        selected_dpi = "unknown"

    logging.info(
        f"✓ Selected {selected_type} variant: "
        f"arch={selected_arch}, "
        f"dpi={selected_dpi}, "
        f"min_android={selected_variant['android_version']}, "
        f"size={selected_variant['file_size']}"
    )

    logging.info(
        f"✓ Variant URL: {download_page_url}"
    )

    # --- STANDARD DOWNLOAD FLOW ---
    try:
        response = _cf_get(download_page_url)
        response.raise_for_status()
        content_size = len(response.content)
        logging.info(f"URL:{response.url} [{content_size}/{content_size}] -> Variant Page")
        soup = BeautifulSoup(response.content, "html.parser")

        sub_url = soup.find('a', class_='downloadButton')
        if sub_url:
            final_download_page_url = base_url + sub_url['href']
            response = _cf_get(final_download_page_url)
            response.raise_for_status()
            content_size = len(response.content)
            logging.info(f"URL:{response.url} [{content_size}/{content_size}] -> Download Page")
            soup = BeautifulSoup(response.content, "html.parser")

            button = soup.find('a', id='download-link')
            if button:
                return base_url + button['href']
    except Exception as e:
        logging.error(f"Error in download flow: {e}")

    return None

def get_architecture_criteria(arch: str) -> dict:
    """Map architecture names to APKMirror criteria"""
    arch_mapping = {
        "arm64-v8a": "arm64-v8a",
        "armeabi-v7a": "armeabi-v7a", 
        "universal": "universal"
    }
    return arch_mapping.get(arch, "universal")
    
def get_latest_version(app_name: str, config: dict) -> str:
    # First try: get from main app page
    try:
        main_url = f"{base_url}/apk/{config['org']}/{config['name']}/"
        response = _cf_get(main_url)
        if response.status_code == 200:
            soup = BeautifulSoup(response.content, "html.parser")
            # Try to find version in the page
            version_elem = soup.find('span', string=re.compile(r'\d+\.\d+'))
            if version_elem:
                version_text = version_elem.text.strip()
                match = re.search(r'(\d+(\.\d+)+)', version_text)
                if match:
                    return match.group(1)
    except:
        pass  # If fails, continue to original method
    
    # Original method (keep exactly as you had it)
    url = f"{base_url}/uploads/?appcategory={config['name']}"
    
    response = _cf_get(url)
    response.raise_for_status()
    content_size = len(response.content)
    logging.info(f"URL:{response.url} [{content_size}/{content_size}] -> \"-\" [1]")
    soup = BeautifulSoup(response.content, "html.parser")

    app_rows = soup.find_all("div", class_="appRow")
    version_pattern = re.compile(r'\d+(\.\d+)*(-[a-zA-Z0-9]+(\.\d+)*)*')

    for row in app_rows:
        title_h5 = row.find("h5", class_="appRowTitle")
        if not title_h5 or not title_h5.a:
            continue
        version_text = title_h5.a.get_text(strip=True) or ""
        if "alpha" not in version_text.lower() and "beta" not in version_text.lower():
            match = version_pattern.search(version_text)
            if match:
                version = match.group()
                version_parts = version.split('.')
                base_version_parts = []
                for part in version_parts:
                    if part.isdigit():
                        base_version_parts.append(part)
                    else:
                        break
                if base_version_parts:
                    base_version = '.'.join(base_version_parts)
                    
                    # Check for build number in parentheses like "32.30.0(1575420)"
                    build_match = re.search(r'\((\d+)\)', version_text)
                    if build_match:
                        build_number = build_match.group(1)
                        return f"{base_version}({build_number})"
                    
                    return base_version

    return None
