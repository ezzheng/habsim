//Maps initialization - wait for Google Maps to load
var map = null;
var clickMarker = null;
var heatmapLayer = null; // Global heatmap layer for Monte Carlo visualization

function initMap() {
    // Check if Google Maps is loaded
    if (typeof google === 'undefined' || !google.maps || !google.maps.Map) {
        // Google Maps not loaded yet, retry after a short delay
        setTimeout(initMap, 100);
        return;
    }
    
    var element = document.getElementById("map");
    if (!element) {
        // Map element not found, retry
        setTimeout(initMap, 100);
        return;
    }
    
    // Initialize map
    map = new google.maps.Map(element, {
        center: new google.maps.LatLng(37.4, -121.5),
        zoom: 9,
        mapTypeId: "OSM",
        zoomControl: false, // Disable default - we'll use custom
        gestureHandling: 'greedy',
        mapTypeControl: false, // Disable default control - we'll use custom
        fullscreenControl: false, // Disable default - we'll use custom
        streetViewControl: false
    });
    
    google.maps.event.addListener(map, 'click', function (event) {
        displayCoordinates(event.latLng);
    });
    
    // Define OSM map type
    map.mapTypes.set("OSM", new google.maps.ImageMapType({
        getTileUrl: function(coord, zoom) {
            // "Wrap" x (longitude) at 180th meridian properly
            // NB: Don't touch coord.x: because coord param is by reference, and changing its x property breaks something in Google's lib
            var tilesPerGlobe = 1 << zoom;
            var x = coord.x % tilesPerGlobe;
            if (x < 0) {
                x = tilesPerGlobe+x;
            }
            // Wrap y (latitude) in a like manner if you want to enable vertical infinite scrolling
            return "https://tile.openstreetmap.org/" + zoom + "/" + x + "/" + coord.y + ".png";
        },
        tileSize: new google.maps.Size(256, 256),
        name: "OpenStreetMap",
        maxZoom: 18
    }));
    
    // Continue with rest of initialization
    initMapControls();
    initFullscreenControl();
}

// Start initialization
initMap();

// Unified control container for Map and Search buttons
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
                        
                        const hasText = searchInput.value.trim().length > 0;
                        const isMobile = window.innerWidth <= 768;
                        const inputRect = searchInput.getBoundingClientRect();
                        
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
                
                searchInput.addEventListener('focus', styleAutocomplete);
                
                searchInput.addEventListener('blur', () => {
                    setTimeout(() => {
                        const pacContainer = document.querySelector('.pac-container');
                        if (pacContainer && !pacContainer.contains(document.activeElement)) {
                            toggleAutocompleteVisibility();
                        }
                    }, 200);
                });
                
                window.addEventListener('resize', styleAutocomplete);
                
                const observer = new MutationObserver((mutations) => {
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

// Custom fullscreen control (desktop only)
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


// Functions for displaying things
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
function updateClickMarker(position) {
    if (clickMarker) {
        clickMarker.setMap(null);
    }
    clickMarker = new google.maps.Marker({
        position: position,
        map: map
    });
}
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
async function habmc(){
    let activemissionurl = "https://stanfordssi.org/transmissions/recent";
    const proxyurl = "https://cors-anywhere.herokuapp.com/";

    await fetch(proxyurl + activemissionurl) // https://cors-anywhere.herokuapp.com/https://example.com
        .then(response => response.text())
        .then(contents => habmcshow(contents))
        .catch(() => console.log("Cant access " + activemissionurl + " response. Blocked by browser?"));
    getTimeremain();
    
}
function toTimestamp(year, month, day, hour, minute) {
    const datum = new Date(year, month - 1, day, hour, minute);
    return datum.getTime() / 1000;
}

function checkNumPos(numlist){
    for (var each in numlist){
        if(isNaN(numlist[each]) || Math.sign(numlist[each]) === -1 || !numlist[each]){
            alert("ATTENTION: All values should be positive and numbers, check your inputs again!");
            return false;
        }
    }
    return true;
}

function checkasc(asc,alt,equil){
    if(alt<equil && asc==="0"){
        alert("ATTENTION: Ascent rate is 0 while balloon altitude is below its descent ready altitude");
        return false;
    }
    return true;
}

function habmcshow(data){
    let jsondata = JSON.parse(data);
    let checkmsn = activeMissions[CURRENT_MISSION];
    for (let transmission in jsondata) {
        //console.log(activeMissions[jsondata[transmission]['mission']]);
        //console.log()

        if(jsondata[transmission]['mission'] === checkmsn){
            console.log(jsondata[transmission]);
            habmcshoweach(jsondata[transmission]);
        }
    }

}

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
