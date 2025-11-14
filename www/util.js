/**
 * Google Maps initialization and utility functions module.
 * 
 * Handles:
 * - Google Maps initialization and configuration
 * - Map controls (map type selector, search, fullscreen)
 * - Coordinate display and marker management
 * - Elevation fetching
 * - Input validation helpers
 * - Active mission data integration
 */

// ============================================================================
// GLOBAL STATE
// ============================================================================

/** Google Maps instance (initialized by initMap) */
var map = null;

/** Click marker showing selected location on map */
var clickMarker = null;

/** Global heatmap layer for Monte Carlo visualization */
var heatmapLayer = null;

/**
 * Initialize Google Maps instance.
 * 
 * Waits for Google Maps API to load, then creates map instance with OpenStreetMap
 * as default map type. Sets up click handler for coordinate selection and initializes
 * custom controls (map type selector, search, fullscreen).
 * 
 * Retries automatically if Google Maps API or map element isn't ready yet.
 * 
 * Side effects:
 * - Sets global map variable
 * - Adds click listener to map
 * - Registers OSM map type
 * - Initializes custom controls
 */
function initMap() {
    // Check if Google Maps API is loaded
    if (typeof google === 'undefined' || !google.maps || !google.maps.Map) {
        // Google Maps not loaded yet, retry after a short delay
        setTimeout(initMap, 100);
        return;
    }
    
    // Check if map element exists in DOM
    var element = document.getElementById("map");
    if (!element) {
        // Map element not found, retry
        setTimeout(initMap, 100);
        return;
    }
    
    // Initialize map with default settings
    // Center: San Francisco Bay Area (37.4°N, -121.5°W)
    // Zoom: 9 (regional view)
    map = new google.maps.Map(element, {
        center: new google.maps.LatLng(37.4, -121.5),
        zoom: 9,
        mapTypeId: "OSM",  // OpenStreetMap (custom map type)
        zoomControl: false,  // Disable default - we'll use custom
        gestureHandling: 'greedy',  // Allow map dragging even when over controls
        mapTypeControl: false,  // Disable default control - we'll use custom
        fullscreenControl: false,  // Disable default - we'll use custom
        streetViewControl: false
    });
    
    // Add click listener to update coordinates when user clicks map
    google.maps.event.addListener(map, 'click', function (event) {
        displayCoordinates(event.latLng);
    });
    
    // Define OpenStreetMap (OSM) as custom map type
    // OSM provides free, open-source map tiles
    map.mapTypes.set("OSM", new google.maps.ImageMapType({
        getTileUrl: function(coord, zoom) {
            // "Wrap" x (longitude) at 180th meridian properly
            // NB: Don't touch coord.x: because coord param is by reference, and changing its x property breaks something in Google's lib
            var tilesPerGlobe = 1 << zoom;  // 2^zoom tiles per globe
            var x = coord.x % tilesPerGlobe;
            if (x < 0) {
                x = tilesPerGlobe + x;  // Handle negative wrap-around
            }
            // Wrap y (latitude) in a like manner if you want to enable vertical infinite scrolling
            return "https://tile.openstreetmap.org/" + zoom + "/" + x + "/" + coord.y + ".png";
        },
        tileSize: new google.maps.Size(256, 256),
        name: "OpenStreetMap",
        maxZoom: 18
    }));
    
    // Continue with rest of initialization
    initMapControls();  // Map type selector and search
    initFullscreenControl();  // Fullscreen button
}

// Start initialization (will retry if Google Maps API isn't loaded yet)
initMap();

/**
 * Initialize custom map controls (map type selector and search).
 * 
 * Creates unified control container with:
 * - Map type selector dropdown (OSM, Roadmap, Satellite, Hybrid, Terrain)
 * - Location search bar with Google Places Autocomplete
 * 
 * Controls are positioned at bottom-left on desktop, top-left on mobile.
 * 
 * Dependencies: Requires global map object to be initialized
 */
function initMapControls() {
    if (!map) return;
    
    // Wait for map to be ready
    google.maps.event.addListenerOnce(map, 'idle', function() {
        // Create unified control container
        const controlsContainer = document.createElement('div');
        controlsContainer.className = 'map-controls-container';
        
        // ===== MAP TYPE CONTROL =====
        const mapTypeControl = document.createElement('div');
        mapTypeControl.className = 'map-type-control';
        mapTypeControl.style.position = 'relative';
        
        // Create map type button
        const mapTypeButton = document.createElement('button');
        mapTypeButton.type = 'button';
        mapTypeButton.className = 'map-control-button';
        mapTypeButton.innerHTML = '🗺️';
        mapTypeButton.title = 'Map type';
        
        // Create dropdown menu
        const mapTypeMenu = document.createElement('div');
        mapTypeMenu.className = 'custom-map-type-menu';
        
        // Map type options
        const mapTypes = [
            { id: 'OSM', label: 'Map', icon: '🗺️' },
            { id: 'roadmap', label: 'Roadmap', icon: '🛣️' },
            { id: 'satellite', label: 'Satellite', icon: '🛰️' },
            { id: 'hybrid', label: 'Hybrid', icon: '🌍' },
            { id: 'terrain', label: 'Terrain', icon: '⛰️' }
        ];
        
        // Create menu items
        mapTypes.forEach((mapType) => {
            const menuItem = document.createElement('button');
            menuItem.type = 'button';
            menuItem.style.cssText = `
                background-color: white;
                border: none;
                border-bottom: 1px solid #e0e0e0;
                padding: 10px 16px;
                text-align: left;
                cursor: pointer;
                font-size: 13px;
                color: #5B5B5B;
                display: flex;
                align-items: center;
                gap: 10px;
                transition: background-color 0.1s;
                font-family: Roboto, Arial, sans-serif;
                width: 100%;
            `;
            menuItem.innerHTML = `<span>${mapType.icon}</span> <span>${mapType.label}</span>`;
            
            menuItem.onmouseenter = () => menuItem.style.backgroundColor = '#f5f5f5';
            menuItem.onmouseleave = () => menuItem.style.backgroundColor = 'white';
            
            menuItem.onclick = () => {
                try {
                    map.setMapTypeId(mapType.id);
                    updateActiveMapType(mapType.id);
                    closeMapTypeMenu();
                } catch(e) {
                    console.warn('Map type not available:', mapType.id);
                }
            };
            
            mapTypeMenu.appendChild(menuItem);
        });
        
        // Remove border from last item
        const lastItem = mapTypeMenu.lastElementChild;
        if (lastItem) {
            lastItem.style.borderBottom = 'none';
        }
        
        // Update active map type indicator
        const updateActiveMapType = (activeId) => {
            const items = mapTypeMenu.querySelectorAll('button');
            items.forEach((item, index) => {
                if (mapTypes[index].id === activeId) {
                    item.style.backgroundColor = '#e8f0fe';
                    item.style.color = '#1a73e8';
                } else {
                    item.style.backgroundColor = 'white';
                    item.style.color = '#5B5B5B';
                }
            });
        };
        
        // Toggle map type menu
        const toggleMapTypeMenu = (e) => {
            e.stopPropagation();
            const isVisible = mapTypeMenu.style.display === 'flex';
            if (isVisible) {
                closeMapTypeMenu();
            } else {
                closeSearchBar(); // Close search if open
                mapTypeMenu.style.display = 'flex';
                updateActiveMapType(map.getMapTypeId());
            }
        };
        
        const closeMapTypeMenu = () => {
            mapTypeMenu.style.display = 'none';
        };
        
        mapTypeButton.onclick = toggleMapTypeMenu;
        
        mapTypeControl.appendChild(mapTypeButton);
        mapTypeControl.appendChild(mapTypeMenu);
        
        // ===== SEARCH CONTROL =====
        const searchControl = document.createElement('div');
        searchControl.className = 'search-control-container';
        
        // Create search button
        const searchButton = document.createElement('button');
        searchButton.type = 'button';
        searchButton.className = 'map-control-button';
        searchButton.innerHTML = '🔍';
        searchButton.title = 'Search location';
        
        // Create search input container
        const searchInputContainer = document.createElement('div');
        searchInputContainer.className = 'search-input-container';
        
        // Create search input
        const searchInput = document.createElement('input');
        searchInput.type = 'text';
        searchInput.className = 'search-input-field';
        searchInput.placeholder = 'Search for a location...';
        
        searchInputContainer.appendChild(searchInput);
        
        let autocomplete = null;
        let autocompleteInitialized = false;
        
        const initAutocomplete = () => {
            if (autocompleteInitialized) return;
            if (!searchInputContainer.classList.contains('expanded')) return;
            
            // Cache input position to prevent autocomplete shifting when modifier keys are pressed
            let cachedInputRect = null;
            let positionLocked = false;
            
            if (typeof google === 'undefined' || !google.maps || !google.maps.places || !google.maps.places.Autocomplete) {
                console.warn('Places library not loaded');
                return;
            }
            
            try {
                // Create autocomplete instance
                autocomplete = new google.maps.places.Autocomplete(searchInput, {
                    types: ['geocode'],
                    fields: ['geometry', 'name', 'formatted_address']
                });
                
                // Bind autocomplete to map bounds for better suggestions
                try {
                    autocomplete.bindTo('bounds', map);
                } catch(e) {
                    // bindTo may not be available in all API versions
                }
                
                const hideContainerIfNoText = () => {
                    const pacContainer = document.querySelector('.pac-container');
                    if (pacContainer && searchInput.value.trim().length === 0) {
                        pacContainer.classList.remove('has-text');
                        pacContainer.style.display = 'none';
                        pacContainer.style.visibility = 'hidden';
                        pacContainer.style.opacity = '0';
                    }
                };
                
                [0, 50, 100, 200].forEach(delay => setTimeout(hideContainerIfNoText, delay));
                
                // Handle place selection
                autocomplete.addListener('place_changed', () => {
                    const place = autocomplete.getPlace();
                    if (!place.geometry) {
                        console.warn('No geometry available for place');
                        return;
                    }
                    
                    const location = place.geometry.location;
                    const lat = location.lat();
                    const lng = location.lng();
                    
                    // Pan and zoom to location (panTo has built-in smooth animation)
                    map.panTo(location);
                    map.setZoom(12);
                    
                    // Update coordinates and marker immediately (before animation completes)
                    displayCoordinates(new google.maps.LatLng(lat, lng));
                    
                    // Close search bar
                    closeSearchBar();
                });
                
                const toggleAutocompleteVisibility = () => {
                    const pacContainer = document.querySelector('.pac-container');
                    if (!pacContainer) return;
                    const hasText = searchInput.value.trim().length > 0;
                    if (hasText) {
                        pacContainer.classList.add('has-text');
                    } else {
                        pacContainer.classList.remove('has-text');
                    }
                };
                
                const styleAutocomplete = () => {
                    if (!searchInputContainer.classList.contains('expanded')) return;
                    
                    requestAnimationFrame(() => {
                        const pacContainer = document.querySelector('.pac-container');
                        if (!pacContainer || !searchInput) return;
                        
                        // Skip repositioning if position is locked and we have a cached position
                        if (positionLocked && cachedInputRect) return;
                        
                        const hasText = searchInput.value.trim().length > 0;
                        const isMobile = window.innerWidth <= 768;
                        
                        // Get input position - use cache if position is locked, otherwise update cache
                        let inputRect;
                        if (positionLocked && cachedInputRect) {
                            inputRect = cachedInputRect;
                        } else {
                            inputRect = searchInput.getBoundingClientRect();
                            cachedInputRect = {
                                left: inputRect.left,
                                top: inputRect.top,
                                bottom: inputRect.bottom,
                                width: inputRect.width
                            };
                        }
                        
                        pacContainer.style.zIndex = '10000';
                        pacContainer.style.position = 'fixed';
                        pacContainer.style.pointerEvents = 'auto';
                        pacContainer.style.display = hasText ? 'block' : 'none';
                        pacContainer.style.visibility = hasText ? 'visible' : 'hidden';
                        pacContainer.style.opacity = hasText ? '1' : '0';
                        pacContainer.style.left = inputRect.left + 'px';
                        pacContainer.style.width = inputRect.width + 'px';
                        
                        if (isMobile) {
                            pacContainer.style.top = (inputRect.bottom + window.scrollY + 5) + 'px';
                            pacContainer.style.bottom = 'auto';
                            pacContainer.style.transform = 'none';
                        } else {
                            pacContainer.style.top = (inputRect.top + window.scrollY - 5) + 'px';
                            pacContainer.style.bottom = 'auto';
                            pacContainer.style.transform = 'translateY(-100%)';
                        }
                        
                        pacContainer.querySelectorAll('.pac-item').forEach(item => {
                            item.style.pointerEvents = 'auto';
                            item.style.cursor = 'pointer';
                        });
                        
                        ['click', 'mousedown', 'touchstart'].forEach(event => {
                            pacContainer.addEventListener(event, e => e.stopPropagation(), true);
                        });
                    });
                };
                
                searchInput.addEventListener('input', () => {
                    toggleAutocompleteVisibility();
                    styleAutocomplete();
                    setTimeout(() => {
                        toggleAutocompleteVisibility();
                        styleAutocomplete();
                    }, 50);
                });
                
                // Lock position when modifier keys are pressed (prevents autocomplete from shifting)
                searchInput.addEventListener('keydown', (e) => {
                    if (e.ctrlKey || e.metaKey || e.altKey) {
                        if (!positionLocked) {
                            // Cache position immediately before any layout changes
                            cachedInputRect = searchInput.getBoundingClientRect();
                            positionLocked = true;
                        }
                    }
                });
                
                searchInput.addEventListener('keyup', (e) => {
                    if (!e.ctrlKey && !e.metaKey && !e.altKey) {
                        positionLocked = false;
                        // Update cache with fresh position after modifier keys released
                        if (document.activeElement === searchInput) {
                            styleAutocomplete();
                        }
                    }
                });
                
                searchInput.addEventListener('blur', () => {
                    positionLocked = false;
                    setTimeout(() => {
                        const pacContainer = document.querySelector('.pac-container');
                        if (pacContainer && !pacContainer.contains(document.activeElement)) {
                            toggleAutocompleteVisibility();
                        }
                    }, 200);
                });
                
                searchInput.addEventListener('focus', () => {
                    positionLocked = false;
                    styleAutocomplete();
                });
                
                window.addEventListener('resize', () => {
                    positionLocked = false;
                    styleAutocomplete();
                });
                
                const observer = new MutationObserver((mutations) => {
                    if (positionLocked) return; // Skip repositioning during modifier key press
                    mutations.forEach((mutation) => {
                        mutation.addedNodes.forEach((node) => {
                            if (node.nodeType === 1) {
                                if (node.classList?.contains('pac-container') || node.querySelector?.('.pac-container')) {
                                    toggleAutocompleteVisibility();
                                    styleAutocomplete();
                                }
                            }
                        });
                    });
                });
                
                observer.observe(document.body, { childList: true, subtree: true });
                
                setTimeout(() => {
                    const pacContainer = document.querySelector('.pac-container');
                    if (pacContainer) {
                        toggleAutocompleteVisibility();
                        styleAutocomplete();
                    }
                }, 100);
                
                autocompleteInitialized = true;
            } catch (e) {
                console.error('Error initializing Places Autocomplete:', e);
            }
        };
        
        const toggleSearchBar = (e) => {
            e.stopPropagation();
            const isExpanded = searchInputContainer.classList.contains('expanded');
            if (isExpanded) {
                closeSearchBar();
            } else {
                closeMapTypeMenu();
                searchInputContainer.classList.add('expanded');
                setTimeout(() => {
                    if (searchInputContainer.classList.contains('expanded') && searchInputContainer.offsetWidth > 0) {
                        initAutocomplete();
                        searchInput.focus();
                    }
                }, 350);
            }
        };
        
        const closeSearchBar = () => {
            // Immediately hide autocomplete dropdown container
            const pacContainer = document.querySelector('.pac-container');
            if (pacContainer) {
                pacContainer.classList.remove('has-text');
                pacContainer.style.display = 'none';
                pacContainer.style.visibility = 'hidden';
                pacContainer.style.opacity = '0';
                pacContainer.style.pointerEvents = 'none';
            }
            
            // Clear input and remove expanded class
            searchInput.value = '';
            searchInput.blur();
            searchInputContainer.classList.remove('expanded');
            
            // Additional cleanup after transition completes to catch any lingering elements
            setTimeout(() => {
                const pacContainerAfter = document.querySelector('.pac-container');
                if (pacContainerAfter) {
                    pacContainerAfter.classList.remove('has-text');
                    pacContainerAfter.style.display = 'none';
                    pacContainerAfter.style.visibility = 'hidden';
                    pacContainerAfter.style.opacity = '0';
                    pacContainerAfter.style.pointerEvents = 'none';
                }
                
                // Also check for any orphaned "Powered by Google" elements
                const poweredByElements = document.querySelectorAll('.pac-logo, [class*="pac-logo"]');
                poweredByElements.forEach(el => {
                    // Only hide if not part of a visible pac-container
                    if (!el.closest('.pac-container') || 
                        (el.closest('.pac-container') && 
                         el.closest('.pac-container').style.display === 'none')) {
                        el.style.display = 'none';
                        el.style.visibility = 'hidden';
                        el.style.opacity = '0';
                    }
                });
            }, 350); // Wait for CSS transition to complete
        };
        
        searchButton.onclick = toggleSearchBar;
        
        // Close search on Escape key
        searchInput.addEventListener('keydown', (e) => {
            if (e.key === 'Escape') {
                closeSearchBar();
            }
        });
        
        searchControl.appendChild(searchButton);
        searchControl.appendChild(searchInputContainer);
        
        // Assemble controls
        controlsContainer.appendChild(mapTypeControl);
        controlsContainer.appendChild(searchControl);
        
        // Close menus when clicking outside
        document.addEventListener('click', (e) => {
            const pacContainer = document.querySelector('.pac-container');
            const clickedInPac = pacContainer && pacContainer.contains(e.target);
            
            if (!controlsContainer.contains(e.target) && !clickedInPac) {
                closeMapTypeMenu();
                closeSearchBar();
            }
        });
        
        // Add to map
        map.controls[google.maps.ControlPosition.BOTTOM_LEFT].push(controlsContainer);
        
        // Initialize active map type
        updateActiveMapType(map.getMapTypeId());
    });
}

/**
 * Initialize custom fullscreen control button.
 * 
 * Creates a fullscreen toggle button that works across browsers (handles
 * vendor prefixes for Chrome, Firefox, Safari, IE). Button is positioned
 * in top-right corner of map.
 * 
 * Dependencies: Requires global map object to be initialized
 */
function initFullscreenControl() {
    if (!map) return;
    
    // Wait for map to be ready
    google.maps.event.addListenerOnce(map, 'idle', function() {
        // Create controls container
        const controlsContainer = document.createElement('div');
        controlsContainer.id = 'custom-map-controls';
        controlsContainer.className = 'custom-fullscreen-container';
        
        // Fullscreen button
        const fullscreenButton = document.createElement('button');
        fullscreenButton.type = 'button';
        fullscreenButton.className = 'custom-fullscreen-button';
        fullscreenButton.innerHTML = '⛶';
        fullscreenButton.title = 'Toggle fullscreen';
        
        // Fullscreen functions
        const isFullscreenSupported = () => !!(document.fullscreenEnabled || document.webkitFullscreenEnabled || document.mozFullScreenEnabled || document.msFullscreenEnabled);
        
        const getFullscreenElement = () => document.fullscreenElement || document.webkitFullscreenElement || document.mozFullScreenElement || document.msFullscreenElement;
        
        const enterFullscreen = () => {
            const mapElement = document.getElementById('map');
            if (!mapElement) return;
            const methods = ['requestFullscreen', 'webkitRequestFullscreen', 'mozRequestFullScreen', 'msRequestFullscreen'];
            for (const method of methods) {
                if (mapElement[method]) {
                    mapElement[method]();
                    break;
                }
            }
        };
        
        const exitFullscreen = () => {
            const methods = ['exitFullscreen', 'webkitExitFullscreen', 'mozCancelFullScreen', 'msExitFullscreen'];
            for (const method of methods) {
                if (document[method]) {
                    document[method]();
                    break;
                }
            }
        };
        
        const updateFullscreenIcon = () => {
            const isFullscreen = !!getFullscreenElement();
            fullscreenButton.innerHTML = '⛶';
            fullscreenButton.title = isFullscreen ? 'Exit fullscreen' : 'Enter fullscreen';
        };
        
        fullscreenButton.onclick = function(e) {
            e.stopPropagation();
            if (!isFullscreenSupported()) {
                console.warn('Fullscreen not supported');
                return;
            }
            const isFullscreen = !!getFullscreenElement();
            if (isFullscreen) exitFullscreen();
            else enterFullscreen();
        };
        
        ['fullscreenchange', 'webkitfullscreenchange', 'mozfullscreenchange', 'MSFullscreenChange'].forEach(event => {
            document.addEventListener(event, updateFullscreenIcon);
        });
        
        controlsContainer.appendChild(fullscreenButton);
        const mapContainer = document.getElementById('map');
        if (mapContainer) mapContainer.appendChild(controlsContainer);
        updateFullscreenIcon();
    });
}


// ============================================================================
// COORDINATE AND MARKER MANAGEMENT
// ============================================================================

/**
 * Update coordinates and marker when user clicks on map.
 * 
 * Called when user clicks anywhere on the map. Updates lat/lon input fields,
 * moves click marker to new location, clears previous visualizations, cancels
 * any in-progress simulation, and fetches elevation for new location.
 * 
 * @param {google.maps.LatLng} pnt - Clicked location (lat/lng object)
 * 
 * Side effects:
 * - Updates lat/lon input fields
 * - Moves click marker
 * - Clears all visualizations (paths, heatmap, contours, markers)
 * - Cancels in-progress simulation
 * - Fetches elevation for new location (debounced)
 */
function displayCoordinates(pnt) {
    var lat = pnt.lat();
    lat = lat.toFixed(4);
    var lng = pnt.lng();
    lng = lng.toFixed(4);
    document.getElementById("lat").value = lat;
    document.getElementById("lon").value = lng;
    updateClickMarker(new google.maps.LatLng(parseFloat(lat), parseFloat(lng)));
    // Clear all visualizations when new location is clicked
    if (typeof clearAllVisualizations === 'function') {
        clearAllVisualizations();
    } else {
        // Fallback if function not available (shouldn't happen)
    clearWaypoints();
    for (path in currpaths) {currpaths[path].setMap(null);}
    currpaths = new Array();
    rawpathcache = new Array();
        if (typeof clearEndPin === 'function') {
            try { clearEndPin(); } catch (e) {}
        }
        if (typeof rawpathcacheModels !== 'undefined') {
            rawpathcacheModels = new Array();
        }
        if (heatmapLayer) {
            try {
                if (heatmapLayer.setMap) {
                    heatmapLayer.setMap(null);
                }
            } catch (e) {
                console.warn('Error clearing heatmap:', e);
            }
            heatmapLayer = null;
        }
        if (typeof clearContours === 'function') {
            clearContours();
        }
    }
    // If a simulation is in progress, cancel it on new click
    if (window.__simRunning && window.__simAbort) {
        try { window.__simAbort.abort(); } catch(e) {}
    }
    // Blank elevation until fresh value is fetched for this location
    var altInput = document.getElementById('alt');
    if (altInput) altInput.value = '';
    // Debounce elevation fetch to avoid bursts on rapid clicks
    if (window.__elevDebounceTimer) {
        try { clearTimeout(window.__elevDebounceTimer); } catch(e) {}
    }
    window.__elevDebounceTimer = setTimeout(() => {
        try { getElev(); } catch(e) {}
    }, 150);
}
/**
 * Update click marker position on map.
 * 
 * Removes old marker (if exists) and creates new marker at specified position.
 * Click marker shows where user has selected coordinates.
 * 
 * @param {google.maps.LatLng} position - New marker position
 * 
 * Side effects: Updates global clickMarker variable
 */
function updateClickMarker(position) {
    // Remove old marker if it exists
    if (clickMarker) {
        clickMarker.setMap(null);
    }
    // Create new marker at clicked position
    clickMarker = new google.maps.Marker({
        position: position,
        map: map
    });
}

/**
 * Fetch ground elevation for current lat/lon coordinates.
 * 
 * Makes API request to backend elevation endpoint and updates altitude input
 * field. Aborts any in-flight requests when a new one starts (prevents race
 * conditions from rapid clicks).
 * 
 * Side effects:
 * - Updates alt input field with elevation value
 * - Sets window.__elevAbort for request cancellation
 */
function getElev() {
    // Abort any in-flight elevation fetch when a new one starts
    if (window.__elevAbort) {
        try { window.__elevAbort.abort(); } catch(e) {}
    }
    window.__elevAbort = new AbortController();

    lat = document.getElementById("lat").value;
    lng = document.getElementById("lon").value;
    fetch(URL_ROOT + "/elev?lat=" + lat + "&lon=" + lng, { signal: window.__elevAbort.signal })
        .then(res => {
            if (!res.ok) throw new Error(`HTTP ${res.status}`);
            return res.json();
        })
        .then((result) => {
            if (typeof result === 'number') {
                document.getElementById("alt").value = Math.round(result);
            } else if (result && result.error) {
                throw new Error(result.error);
            } else {
                var val = parseFloat(result);
                document.getElementById("alt").value = isNaN(val) ? result : Math.round(val);
            }
        })
        .catch(err => {
            if (err && (err.name === 'AbortError' || err.message === 'The operation was aborted.')) {
                // ignore aborted fetch
                return;
            }
            console.error('Elevation fetch failed', err);
            alert('Failed to fetch ground elevation. Please try again.');
        })
        .finally(() => {
            window.__elevAbort = null;
        });
}
/**
 * Calculate and display remaining time until burst or landing.
 * 
 * Calculates time remaining based on current altitude:
 * - If below burst altitude: calculates ascent time remaining
 * - If at/above burst altitude: calculates descent time remaining (requires ground elevation)
 * 
 * Displays result in "timeremain" element with format "X.XX hr ascent/descent remaining".
 * 
 * Dependencies: Requires alt, equil, asc, desc input fields
 */
function getTimeremain() {
    const remainNode = document.getElementById("timeremain");
    if (!remainNode) return;
    
    const alt = parseFloat(document.getElementById("alt").value);
    const eqalt = parseFloat(document.getElementById("equil").value);
    
    if (alt < eqalt) {
        const ascr = parseFloat(document.getElementById("asc").value);
        const time = (eqalt - alt) / (3600 * ascr);
        remainNode.textContent = time.toFixed(2) + " hr ascent remaining";
    } else {
        const descr = parseFloat(document.getElementById("desc").value);
        const lat = document.getElementById("lat").value;
        const lng = document.getElementById("lon").value;
        fetch(URL_ROOT + "/elev?lat=" + lat + "&lon=" + lng)
            .then(res => res.json())
            .then(ground => {
                const time = (alt - ground) / (3600 * descr);
                remainNode.textContent = time.toFixed(2) + " hr descent remaining";
            })
            .catch(err => {
                console.error('Elevation fetch failed', err);
                alert('Failed to fetch ground elevation for remaining time.');
            });
    }
}
// ============================================================================
// ACTIVE MISSION INTEGRATION
// ============================================================================

/**
 * Fetch active mission data from Stanford SSI transmissions API.
 * 
 * Uses CORS proxy to fetch recent transmission data and display it on the map.
 * Updates simulation parameters with mission data and calculates remaining time.
 * 
 * Note: Requires activeMissions and CURRENT_MISSION global variables to be defined.
 * 
 * Side effects:
 * - Calls habmcshow() to process and display mission data
 * - Calls getTimeremain() to update time remaining display
 */
async function habmc(){
    let activemissionurl = "https://stanfordssi.org/transmissions/recent";
    const proxyurl = "https://cors-anywhere.herokuapp.com/";

    await fetch(proxyurl + activemissionurl)
        .then(response => response.text())
        .then(contents => habmcshow(contents))
        .catch(() => console.log("Can't access " + activemissionurl + " response. Blocked by browser?"));
    getTimeremain();
}

/**
 * Process and display active mission transmission data.
 * 
 * Parses JSON data from transmissions API and finds matching mission.
 * Calls habmcshoweach() for each matching transmission.
 * 
 * @param {string} data - JSON string from transmissions API
 * 
 * Dependencies: Requires activeMissions and CURRENT_MISSION global variables
 */
function habmcshow(data){
    let jsondata = JSON.parse(data);
    let checkmsn = activeMissions[CURRENT_MISSION];
    for (let transmission in jsondata) {
        if(jsondata[transmission]['mission'] === checkmsn){
            console.log(jsondata[transmission]);
            habmcshoweach(jsondata[transmission]);
        }
    }
}

/**
 * Display individual mission transmission on map and update simulation parameters.
 * 
 * Parses transmission data and:
 * - Updates date/time inputs (converts to UTC)
 * - Updates lat/lon coordinates
 * - Creates circle marker on map showing transmission location
 * - Updates altitude and ascent/descent rates based on transmission data
 * 
 * @param {Object} data2 - Transmission data object with Human Time, latitude, longitude, altitude_gps, etc.
 * 
 * Side effects:
 * - Updates date/time, lat/lon, alt, asc/desc/equil input fields
 * - Creates Google Maps Circle marker on map
 * - Pans map to transmission location
 */
function habmcshoweach(data2) {
    const datetime = data2["Human Time"];
    const res = datetime.substring(0, 11).split("-");
    const res2 = datetime.substring(11, 20).split(":");
    let hourutc = parseInt(res2[0]) + 7;
    
    if (hourutc >= 24) {
        hourutc -= 24;
        document.getElementById("day").value = parseInt(res[2]) + 1;
    } else {
        document.getElementById("day").value = parseInt(res[2]);
    }
    
    document.getElementById("hr").value = hourutc;
    document.getElementById("mn").value = parseInt(res2[1]);
    document.getElementById("yr").value = parseInt(res[0]);
    document.getElementById("mo").value = parseInt(res[1]);
    
    const hrMobile = document.getElementById("hr-mobile");
    const mnMobile = document.getElementById("mn-mobile");
    if (hrMobile) hrMobile.value = hourutc;
    if (mnMobile) mnMobile.value = parseInt(res2[1]);
    
    const lat = parseFloat(data2["latitude"]);
    const lon = parseFloat(data2["longitude"]);
    document.getElementById("lat").value = lat;
    document.getElementById("lon").value = lon;
    
    const position = { lat, lng: lon };
    const circle = new google.maps.Circle({
        strokeColor: '#FF0000',
        strokeOpacity: 0.8,
        strokeWeight: 2,
        fillColor: '#FF0000',
        fillOpacity: 0.35,
        map: map,
        center: position,
        radius: 5000,
        clickable: true
    });
    
    const infowindow = new google.maps.InfoWindow({
        content: "Altitude: " + data2["altitude_gps"] + " Ground speed: " + data2["groundSpeed"] + data2["direction"] + " Ascent rate " + data2["ascentRate"]
    });
    
    circle.addListener("mouseover", () => {
        infowindow.setPosition(circle.getCenter());
        infowindow.open(map);
    });
    circle.addListener("mouseout", () => infowindow.close(map));
    map.panTo(new google.maps.LatLng(lat, lon));
    
    const alt = parseFloat(data2["altitude_gps"]);
    document.getElementById("alt").value = alt;
    const rate = parseFloat(data2["ascentRate"]);
    
    if (rate > 0) {
        document.getElementById("asc").value = rate;
    } else {
        document.getElementById("equil").value = alt;
        document.getElementById("desc").value = -rate;
        document.getElementById("eqtime").value = 0;
    }
}

// ============================================================================
// VALIDATION HELPERS
// ============================================================================

/**
 * Convert date/time components to Unix timestamp (seconds since epoch).
 * 
 * @param {number} year - Year (e.g., 2025)
 * @param {number} month - Month (1-12)
 * @param {number} day - Day of month (1-31)
 * @param {number} hour - Hour (0-23)
 * @param {number} minute - Minute (0-59)
 * @returns {number} Unix timestamp in seconds
 */
function toTimestamp(year, month, day, hour, minute) {
    const datum = new Date(year, month - 1, day, hour, minute);
    return datum.getTime() / 1000;
}

/**
 * Validate that all values in array are positive numbers.
 * 
 * Checks each value in the array to ensure it's:
 * - A valid number (not NaN)
 * - Positive (not negative or zero)
 * - Truthy (not null, undefined, empty string, etc.)
 * 
 * @param {Array} numlist - Array of values to validate
 * @returns {boolean} True if all values are positive numbers, false otherwise
 */
function checkNumPos(numlist){
    for (var each in numlist){
        if(isNaN(numlist[each]) || Math.sign(numlist[each]) === -1 || !numlist[each]){
            alert("ATTENTION: All values should be positive and numbers, check your inputs again!");
            return false;
        }
    }
    return true;
}

/**
 * Validate ascent rate is not zero when balloon is below burst altitude.
 * 
 * Prevents invalid simulation state where balloon is below burst altitude
 * but has zero ascent rate (would never reach burst altitude).
 * 
 * @param {string|number} asc - Ascent rate value
 * @param {string|number} alt - Current altitude
 * @param {string|number} equil - Burst/equilibrium altitude
 * @returns {boolean} True if valid, false if ascent rate is 0 while below burst altitude
 */
function checkasc(asc,alt,equil){
    if(alt<equil && asc==="0"){
        alert("ATTENTION: Ascent rate is 0 while balloon altitude is below its descent ready altitude");
        return false;
    }
    return true;
}
