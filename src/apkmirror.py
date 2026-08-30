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
    
    # --- VARIANT FINDER ---
    #
    # Final automatic selection rules:
    #
    # 1. Architecture priority:
    #       arm64-v8a
    #       universal (only if arm64-v8a does not exist)
    #
    # 2. DPI:
    #       - If an exact DPI range exists, prefer the most specific
    #         range (e.g. 480-640dpi over 120-640dpi).
    #       - Otherwise choose the highest DPI available.
    #       - If no suitable DPI is found, use nodpi.
    #
    # 3. If multiple variants are identical:
    #       - Minimum Android version closest to Android 12, then lower.
    #       - Smaller file size.
    #       - First variant on the page as final fallback.
    #
    # 4. Accepted types:
    #       APK and BUNDLE
    #
    # 5. Never intentionally select armeabi-v7a, x86 or x86_64.

    rows = found_soup.find_all(
        'div',
        class_='table-row headerFont'
    )

    download_page_url = None

    requested_arch = str(
        target_arch or config.get('arch', 'arm64-v8a')
    ).strip().lower()

    requested_dpi = str(
        config.get('dpi', '')
    ).strip().lower()

    # ---------------------------------------------------------
    # Parse optional DPI range.
    #
    # If dpi is empty, the range is considered automatic.
    # ---------------------------------------------------------

    dpi_range_match = re.search(
        r'(\d+)\s*-\s*(\d+)\s*dpi',
        requested_dpi,
        re.IGNORECASE
    )

    if dpi_range_match:
        configured_dpi_min = int(
            dpi_range_match.group(1)
        )
        configured_dpi_max = int(
            dpi_range_match.group(2)
        )
        automatic_dpi = False
    else:
        single_dpi_match = re.search(
            r'(\d+)\s*dpi',
            requested_dpi,
            re.IGNORECASE
        )

        if single_dpi_match:
            configured_dpi_min = int(
                single_dpi_match.group(1)
            )
            configured_dpi_max = int(
                single_dpi_match.group(1)
            )
            automatic_dpi = False
        else:
            configured_dpi_min = None
            configured_dpi_max = None
            automatic_dpi = True

    # ---------------------------------------------------------
    # Android version helper
    # ---------------------------------------------------------

    android_version_map = {
        "android 16": 16,
        "android 15": 15,
        "android 14": 14,
        "android 13": 13,
        "android 12": 12,
        "android 11": 11,
        "android 10": 10,
        "android 9": 9,
        "android 8.1": 8.1,
        "android 8": 8,
        "android 7.1": 7.1,
        "android 7": 7,
        "android 6": 6,
        "android 5.1": 5.1,
        "android 5": 5,
    }

    def extract_android_version(text):
        lower = text.lower()

        # Android 12+
        matches = re.findall(
            r'android\s+(\d+(?:\.\d+)?)',
            lower,
            re.IGNORECASE
        )

        if matches:
            try:
                return max(
                    float(value)
                    for value in matches
                )
            except ValueError:
                pass

        for name, value in android_version_map.items():
            if name in lower:
                return value

        # APKMirror sometimes uses API levels.
        api_match = re.search(
            r'(?:android\s+)?api[-\s]?(\d+)',
            lower,
            re.IGNORECASE
        )

        if api_match:
            api = int(api_match.group(1))

            api_map = {
                35: 15,
                34: 14,
                33: 13,
                32: 12,
                31: 12,
                30: 11,
                29: 10,
                28: 9,
                27: 8.1,
                26: 8,
                25: 7.1,
                24: 7,
                23: 6,
                22: 5.1,
                21: 5,
            }

            return api_map.get(api)

        return None

    # ---------------------------------------------------------
    # File size helper
    # ---------------------------------------------------------

    def extract_size(text):
        lower = text.lower()

        matches = re.findall(
            r'(\d+(?:\.\d+)?)\s*(gb|mb|kb|b)\b',
            lower,
            re.IGNORECASE
        )

        if not matches:
            return float('inf')

        sizes = []

        for value, unit in matches:
            value = float(value)

            if unit == 'gb':
                value *= 1024 * 1024 * 1024
            elif unit == 'mb':
                value *= 1024 * 1024
            elif unit == 'kb':
                value *= 1024
            else:
                pass

            sizes.append(value)

        return min(sizes) if sizes else float('inf')

    # ---------------------------------------------------------
    # Extract one APKMirror variant
    # ---------------------------------------------------------

    def extract_variant(row, index):

        text = row.get_text(
            " ",
            strip=True
        )

        lower = text.lower()

        # APK
        is_apk = bool(
            re.search(
                r'\bAPK\b',
                text,
                re.IGNORECASE
            )
        )

        # BUNDLE / AAB
        is_bundle = bool(
            re.search(
                r'\b(?:BUNDLE|APK\s+BUNDLE|AAB)\b',
                text,
                re.IGNORECASE
            )
        )

        if not (is_apk or is_bundle):
            return None

        # -----------------------------------------------------
        # Architecture detection
        # -----------------------------------------------------

        architectures = set()

        architecture_matches = re.findall(
            r'\b(?:arm64-v8a|armeabi-v7a|x86_64|x86|universal)\b',
            lower,
            re.IGNORECASE
        )

        for architecture in architecture_matches:
            architectures.add(
                architecture.lower()
            )

        # -----------------------------------------------------
        # DPI ranges
        # -----------------------------------------------------

        dpi_ranges = []

        for low, high in re.findall(
            r'(\d+)\s*-\s*(\d+)\s*dpi',
            lower,
            re.IGNORECASE
        ):
            dpi_ranges.append(
                (
                    int(low),
                    int(high)
                )
            )

        # -----------------------------------------------------
        # Individual DPI values
        # -----------------------------------------------------

        dpis = []

        for match in re.finditer(
            r'(\d+)\s*dpi',
            lower,
            re.IGNORECASE
        ):
            dpi = int(
                match.group(1)
            )

            # Do not add the two sides of a range separately.
            before = lower[
                max(0, match.start() - 10):
                match.start()
            ]

            if re.search(
                r'\d+\s*-\s*$',
                before
            ):
                continue

            dpis.append(dpi)

        dpis = sorted(
            set(dpis),
            reverse=True
        )

        # -----------------------------------------------------
        # nodpi
        # -----------------------------------------------------

        is_nodpi = bool(
            re.search(
                r'\bnodpi\b',
                lower,
                re.IGNORECASE
            )
        )

        # -----------------------------------------------------
        # Minimum Android version
        # -----------------------------------------------------

        android_version = extract_android_version(
            text
        )

        # -----------------------------------------------------
        # File size
        # -----------------------------------------------------

        file_size = extract_size(
            text
        )

        # -----------------------------------------------------
        # Download link
        # -----------------------------------------------------

        href_element = row.find(
            'a',
            class_='accent_color'
        )

        if not href_element:
            return None

        href = href_element.get(
            'href'
        )

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

    # ---------------------------------------------------------
    # Build variants
    # ---------------------------------------------------------

    variants = []

    for index, row in enumerate(rows):

        variant = extract_variant(
            row,
            index
        )

        if variant:
            variants.append(
                variant
            )

    # ---------------------------------------------------------
    # DPI scoring
    # ---------------------------------------------------------

    def dpi_score(variant):

        # -----------------------------------------------------
        # nodpi is deliberately NOT considered here.
        # It is the fallback after normal DPI selection.
        # -----------------------------------------------------

        # =====================================================
        # Automatic DPI
        #
        # No dpi configured:
        #
        # Prefer a range first.
        # More specific/narrower range wins.
        # If ranges are identical, higher upper bound wins.
        # =====================================================

        if automatic_dpi:

            if variant["dpi_ranges"]:

                best_range = min(
                    variant["dpi_ranges"],
                    key=lambda r: (
                        r[1] - r[0],
                        -r[1],
                        -r[0]
                    )
                )

                low, high = best_range

                return (
                    3,
                    -(high - low),
                    high,
                    high
                )

            # Then individual DPI.
            if variant["dpis"]:

                return (
                    2,
                    0,
                    max(variant["dpis"]),
                    max(variant["dpis"])
                )

            return None

        # =====================================================
        # Configured DPI range
        # =====================================================

        if variant["dpi_ranges"]:

            exact = []

            for low, high in variant["dpi_ranges"]:

                if (
                    low == configured_dpi_min
                    and high == configured_dpi_max
                ):
                    exact.append(
                        (low, high)
                    )

            if exact:

                low, high = exact[0]

                return (
                    4,
                    0,
                    high,
                    high
                )

        # -----------------------------------------------------
        # Individual DPI inside configured range
        # -----------------------------------------------------

        inside = [
            dpi
            for dpi in variant["dpis"]
            if (
                configured_dpi_min
                <= dpi
                <= configured_dpi_max
            )
        ]

        if inside:

            best = max(
                inside
            )

            return (
                3,
                best,
                best,
                best
            )

        # -----------------------------------------------------
        # DPI outside configured range
        # -----------------------------------------------------

        outside = [
            dpi
            for dpi in variant["dpis"]
            if not (
                configured_dpi_min
                <= dpi
                <= configured_dpi_max
            )
        ]

        if outside:

            best = max(
                outside
            )

            return (
                2,
                best,
                best,
                best
            )

        return None

    # ---------------------------------------------------------
    # Select variant
    # ---------------------------------------------------------

    def select_best_variant(
        architecture
    ):

        architecture = architecture.lower()

        matching = [
            variant
            for variant in variants
            if architecture
            in variant["architectures"]
        ]

        if not matching:
            return None

        # -----------------------------------------------------
        # Remove nodpi temporarily.
        # nodpi is only used as fallback.
        # -----------------------------------------------------

        normal_variants = [
            variant
            for variant in matching
            if not variant["is_nodpi"]
        ]

        # -----------------------------------------------------
        # Normal DPI selection
        # -----------------------------------------------------

        scored = []

        for variant in normal_variants:

            score = dpi_score(
                variant
            )

            if score is not None:

                scored.append(
                    (
                        score,
                        variant
                    )
                )

        if scored:

            # Highest DPI/range priority first.
            scored.sort(
                key=lambda item: (
                    item[0],
                    1 if item[1]["is_apk"]
                    else 0,
                    item[1]["index"]
                ),
                reverse=True
            )

            best_score = scored[0][0]

            tied = [
                variant
                for score, variant in scored
                if score == best_score
            ]

            # -------------------------------------------------
            # Tie breaker 1:
            # Minimum Android version closest to Android 12,
            # but never above Android 12.
            # -------------------------------------------------

            if len(tied) > 1:

                below_12 = [
                    variant
                    for variant in tied
                    if (
                        variant["android_version"]
                        is not None
                        and variant["android_version"] <= 12
                    )
                ]

                if below_12:

                    below_12.sort(
                        key=lambda variant: (
                            variant["android_version"],
                            -variant["file_size"],
                            -variant["index"]
                        ),
                        reverse=True
                    )

                    tied = [
                        below_12[0]
                    ]

                else:

                    known_versions = [
                        variant
                        for variant in tied
                        if variant["android_version"]
                        is not None
                    ]

                    if known_versions:

                        known_versions.sort(
                            key=lambda variant: (
                                abs(
                                    variant["android_version"]
                                    - 12
                                ),
                                -variant["file_size"],
                                -variant["index"]
                            )
                        )

                        tied = [
                            known_versions[0]
                        ]

            # -------------------------------------------------
            # Tie breaker 2:
            # Smaller file size.
            # -------------------------------------------------

            if len(tied) > 1:

                tied.sort(
                    key=lambda variant: (
                        variant["file_size"],
                        variant["index"]
                    )
                )

                tied = [
                    tied[0]
                ]

            # -------------------------------------------------
            # Tie breaker 3:
            # First variant on page.
            # -------------------------------------------------

            return tied[0]

        # -----------------------------------------------------
        # No normal DPI variant:
        # fallback to nodpi.
        # -----------------------------------------------------

        nodpi_variants = [
            variant
            for variant in matching
            if variant["is_nodpi"]
        ]

        if nodpi_variants:

            # Same tie-breaking logic for nodpi.
            nodpi_variants.sort(
                key=lambda variant: (
                    0
                    if variant["android_version"]
                    is None
                    else abs(
                        variant["android_version"]
                        - 12
                    ),
                    variant["file_size"],
                    variant["index"]
                )
            )

            return nodpi_variants[0]

        # -----------------------------------------------------
        # Absolute final fallback:
        # Any APK/BUNDLE for this architecture.
        # This prevents DPI from causing failure.
        # -----------------------------------------------------

        if matching:

            matching.sort(
                key=lambda variant: (
                    variant["file_size"],
                    variant["index"]
                )
            )

            return matching[0]

        return None

    # =========================================================
    # STEP 1
    # arm64-v8a
    # =========================================================

    selected_variant = select_best_variant(
        "arm64-v8a"
    )

    if selected_variant:

        selected_arch = "arm64-v8a"

    else:

        # =====================================================
        # STEP 2
        # universal ONLY when arm64-v8a doesn't exist
        # =====================================================

        arm64_exists = any(
            "arm64-v8a"
            in variant["architectures"]
            for variant in variants
        )

        if not arm64_exists:

            logging.info(
                f"No arm64-v8a variant exists for "
                f"{app_name} {version}; trying universal"
            )

            selected_variant = select_best_variant(
                "universal"
            )

            selected_arch = "universal"

        else:

            logging.error(
                f"arm64-v8a exists for "
                f"{app_name} {version}, but no usable "
                f"variant was found."
            )

            return None

    # =========================================================
    # STEP 3
    # Final selection
    # =========================================================

    if selected_variant:

        href = selected_variant["href"]

        download_page_url = (
            href
            if href.startswith("http")
            else base_url + href
        )

        selected_type = (
            "APK"
            if selected_variant["is_apk"]
            else "BUNDLE"
        )

        selected_dpi = (
            "nodpi"
            if selected_variant["is_nodpi"]
            else (
                selected_variant["dpis"][0]
                if selected_variant["dpis"]
                else (
                    selected_variant["dpi_ranges"][0]
                    if selected_variant["dpi_ranges"]
                    else "unknown"
                )
            )
        )

        logging.info(
            f"✓ Selected {selected_type} variant: "
            f"arch={selected_arch}, "
            f"dpi={selected_dpi}, "
            f"min_android="
            f"{selected_variant['android_version']}, "
            f"size="
            f"{selected_variant['file_size']}"
        )

        logging.info(
            f"✓ Variant row: "
            f"{selected_variant['text'][:300]}"
        )

    else:

        logging.error(
            f"No APK/BUNDLE variant found for "
            f"{app_name} {version}"
        )

        return None
    
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
