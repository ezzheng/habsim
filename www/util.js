//Maps initialization
var element = document.getElementById("map");
var map = new google.maps.Map(element, {
    center: new google.maps.LatLng(37.4, -121.5),
    zoom: 9,
    mapTypeId: "OSM",
    zoomControl: false, // Disable default - we'll use custom
    gestureHandling: 'greedy',
    mapTypeControl: false, // Disable default control - we'll use custom
    fullscreenControl: false, // Disable default - we'll use custom
    streetViewControl: false
});
var clickMarker = null;
var heatmapLayer = null; // Global heatmap layer for Monte Carlo visualization
google.maps.event.addListener(map, 'click', function (event) {
    displayCoordinates(event.latLng);
});

// Unified control container for Map and Search buttons
(function() {
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
        
        // Initialize Places Autocomplete - only when input is visible
        let autocomplete = null;
        let autocompleteInitialized = false;
        
        const initAutocomplete = () => {
            // Only initialize if not already done and input is visible
            if (autocompleteInitialized) return;
            
            // Check if input is actually visible (expanded)
            if (!searchInputContainer.classList.contains('expanded')) {
                return;
            }
            
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
                
                // Immediately hide the container if it gets created (prevents initial flicker)
                // Check multiple times as Google may create it asynchronously
                const hideContainerIfNoText = () => {
                    const pacContainer = document.querySelector('.pac-container');
                    if (pacContainer && searchInput.value.trim().length === 0) {
                        pacContainer.classList.remove('has-text');
                        pacContainer.style.display = 'none';
                        pacContainer.style.visibility = 'hidden';
                        pacContainer.style.opacity = '0';
                    }
                };
                
                // Check immediately and after short delays
                setTimeout(hideContainerIfNoText, 0);
                setTimeout(hideContainerIfNoText, 50);
                setTimeout(hideContainerIfNoText, 100);
                setTimeout(hideContainerIfNoText, 200);
                
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
                    
                    // Pan and zoom to location
                    map.panTo(location);
                    map.setZoom(12);
                    
                    // Update coordinates and marker
                    displayCoordinates(new google.maps.LatLng(lat, lng));
                    
                    // Close search bar
                    closeSearchBar();
                });
                
                // Function to show/hide autocomplete based on input text
                const toggleAutocompleteVisibility = () => {
                    const pacContainer = document.querySelector('.pac-container');
                    if (!pacContainer) return;
                    
                    // Only show if input has text
                    const hasText = searchInput.value.trim().length > 0;
                    if (hasText) {
                        pacContainer.classList.add('has-text');
                    } else {
                        pacContainer.classList.remove('has-text');
                    }
                };
                
                // Style and position autocomplete dropdown based on screen size
                const styleAutocomplete = () => {
                    // Only style if search bar is expanded
                    if (!searchInputContainer.classList.contains('expanded')) {
                        return;
                    }
                    
                    // Wait for pac-container to be created by Google
                    requestAnimationFrame(() => {
                        const pacContainer = document.querySelector('.pac-container');
                        if (pacContainer && searchInput) {
                            // Check if input has text before showing
                            const hasText = searchInput.value.trim().length > 0;
                            
                            const isMobile = window.innerWidth <= 768;
                            const inputRect = searchInput.getBoundingClientRect();
                            
                            // Ensure high z-index, visibility, and pointer events
                            pacContainer.style.zIndex = '10000';
                            pacContainer.style.position = 'fixed';
                            pacContainer.style.pointerEvents = 'auto'; // Enable clicks on container
                            
                            // Only show if input has text
                            if (hasText) {
                                pacContainer.style.display = 'block';
                                pacContainer.style.visibility = 'visible';
                                pacContainer.style.opacity = '1';
                            } else {
                                pacContainer.style.display = 'none';
                                pacContainer.style.visibility = 'hidden';
                                pacContainer.style.opacity = '0';
                            }
                            
                            if (isMobile) {
                                // Mobile: dropdown appears below input
                                pacContainer.style.top = (inputRect.bottom + window.scrollY + 5) + 'px';
                                pacContainer.style.bottom = 'auto';
                                pacContainer.style.transform = 'none';
                            } else {
                                // Desktop: dropdown appears above input (dropup)
                                // Calculate distance from top of viewport to top of input
                                const distanceFromTop = inputRect.top + window.scrollY;
                                // Position dropdown above input
                                pacContainer.style.top = (distanceFromTop - 5) + 'px';
                                pacContainer.style.bottom = 'auto';
                                pacContainer.style.transform = 'translateY(-100%)';
                            }
                            pacContainer.style.left = inputRect.left + 'px';
                            pacContainer.style.width = inputRect.width + 'px';
                            
                            // Ensure all suggestion items can receive clicks
                            const pacItems = pacContainer.querySelectorAll('.pac-item');
                            pacItems.forEach(item => {
                                item.style.pointerEvents = 'auto';
                                item.style.cursor = 'pointer';
                            });
                            
                            // Ensure autocomplete container blocks map clicks
                            // Add click handler to prevent event propagation
                            pacContainer.addEventListener('click', (e) => {
                                e.stopPropagation(); // Prevent click from reaching map
                            }, true); // Use capture phase to catch early
                            
                            // Also prevent mousedown/touchstart to be thorough
                            pacContainer.addEventListener('mousedown', (e) => {
                                e.stopPropagation();
                            }, true);
                            
                            pacContainer.addEventListener('touchstart', (e) => {
                                e.stopPropagation();
                            }, true);
                        }
                    });
                };
                
                // Monitor input changes to show/hide autocomplete
                searchInput.addEventListener('input', () => {
                    toggleAutocompleteVisibility();
                    styleAutocomplete();
                    // Also check after a short delay to catch delayed rendering
                    setTimeout(() => {
                        toggleAutocompleteVisibility();
                        styleAutocomplete();
                    }, 50);
                });
                
                // Don't show on focus - only on input
                searchInput.addEventListener('focus', () => {
                    // Just style positioning, but don't show container yet
                    styleAutocomplete();
                });
                
                // Hide when input loses focus (but keep it if user clicked a suggestion)
                searchInput.addEventListener('blur', (e) => {
                    // Small delay to allow click events on suggestions to fire first
                    setTimeout(() => {
                        const pacContainer = document.querySelector('.pac-container');
                        if (pacContainer && !pacContainer.contains(document.activeElement)) {
                            toggleAutocompleteVisibility();
                        }
                    }, 200);
                });
                
                window.addEventListener('resize', styleAutocomplete);
                
                // Use MutationObserver to watch for pac-container creation and hide it initially
                // This prevents the flicker when Google creates the container on focus
                const observer = new MutationObserver((mutations) => {
                    mutations.forEach((mutation) => {
                        mutation.addedNodes.forEach((node) => {
                            if (node.nodeType === 1) { // Element node
                                // Check if the added node is pac-container or contains it
                                if (node.classList && node.classList.contains('pac-container')) {
                                    // Immediately hide if input has no text
                                    toggleAutocompleteVisibility();
                                    styleAutocomplete();
                                } else if (node.querySelector && node.querySelector('.pac-container')) {
                                    const pacContainer = node.querySelector('.pac-container');
                                    if (pacContainer) {
                                        toggleAutocompleteVisibility();
                                        styleAutocomplete();
                                    }
                                }
                            }
                        });
                    });
                });
                
                // Start observing the document body for new elements
                observer.observe(document.body, {
                    childList: true,
                    subtree: true
                });
                
                // Also check immediately in case container already exists
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
        
        // Toggle search bar
        const toggleSearchBar = (e) => {
            e.stopPropagation();
            const isExpanded = searchInputContainer.classList.contains('expanded');
            if (isExpanded) {
                closeSearchBar();
            } else {
                closeMapTypeMenu(); // Close map menu if open
                searchInputContainer.classList.add('expanded');
                
                // Wait for expansion animation to complete, then initialize autocomplete
                // This ensures the input is visible before autocomplete attaches
                setTimeout(() => {
                    // Double-check input is visible before initializing
                    if (searchInputContainer.classList.contains('expanded') && 
                        searchInputContainer.offsetWidth > 0) {
                        initAutocomplete();
                        searchInput.focus();
                    }
                }, 350); // Wait for CSS transition (300ms) + small buffer
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
})();

// Custom fullscreen control (desktop only)
(function() {
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
        function isFullscreenSupported() {
            return !!(document.fullscreenEnabled || 
                     document.webkitFullscreenEnabled || 
                     document.mozFullScreenEnabled || 
                     document.msFullscreenEnabled);
        }
        
        function getFullscreenElement() {
            return document.fullscreenElement || 
                   document.webkitFullscreenElement || 
                   document.mozFullScreenElement || 
                   document.msFullscreenElement;
        }
        
        function enterFullscreen() {
            const mapElement = document.getElementById('map');
            if (!mapElement) return;
            
            if (mapElement.requestFullscreen) {
                mapElement.requestFullscreen();
            } else if (mapElement.webkitRequestFullscreen) {
                mapElement.webkitRequestFullscreen();
            } else if (mapElement.mozRequestFullScreen) {
                mapElement.mozRequestFullScreen();
            } else if (mapElement.msRequestFullscreen) {
                mapElement.msRequestFullscreen();
            }
        }
        
        function exitFullscreen() {
            if (document.exitFullscreen) {
                document.exitFullscreen();
            } else if (document.webkitExitFullscreen) {
                document.webkitExitFullscreen();
            } else if (document.mozCancelFullScreen) {
                document.mozCancelFullScreen();
            } else if (document.msExitFullscreen) {
                document.msExitFullscreen();
            }
        }
        
        function updateFullscreenIcon() {
            const isFullscreen = !!getFullscreenElement();
            fullscreenButton.innerHTML = isFullscreen ? '⛶' : '⛶';
            fullscreenButton.title = isFullscreen ? 'Exit fullscreen' : 'Enter fullscreen';
        }
        
        fullscreenButton.onclick = function(e) {
            e.stopPropagation();
            if (!isFullscreenSupported()) {
                console.warn('Fullscreen not supported');
                return;
            }
            
            const isFullscreen = !!getFullscreenElement();
            if (isFullscreen) {
                exitFullscreen();
            } else {
                enterFullscreen();
            }
        };
        
        // Listen for fullscreen changes
        const fullscreenEvents = ['fullscreenchange', 'webkitfullscreenchange', 'mozfullscreenchange', 'MSFullscreenChange'];
        fullscreenEvents.forEach(function(event) {
            document.addEventListener(event, updateFullscreenIcon);
        });
        
        // Add fullscreen button
        controlsContainer.appendChild(fullscreenButton);
        
        // Add to map container
        const mapContainer = document.getElementById('map');
        if (mapContainer) {
            mapContainer.appendChild(controlsContainer);
        }
        
        // Initialize fullscreen icon
        updateFullscreenIcon();
    });
})();


//Define OSM map type pointing at the OpenStreetMap tile server
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
    var remainNode = document.getElementById("timeremain");
    if (!remainNode) {
        return;
    }
    alt = document.getElementById("alt").value;
    eqalt = document.getElementById("equil").value;
    if (parseFloat(alt) < parseFloat(eqalt)) {
        ascr = document.getElementById("asc").value;
        console.log(alt,eqalt,ascr);
        time = (eqalt - alt)/(3600*ascr);
        console.log(time)
        remainNode.textContent = time.toFixed(2) + " hr ascent remaining"
    }
    else {
        descr = document.getElementById("desc").value;
        lat = document.getElementById("lat").value;
        lng = document.getElementById("lon").value;
        fetch(URL_ROOT + "/elev?lat=" + lat + "&lon=" + lng)
            .then(res => res.json())
            .then((ground) => {
                time = (alt - ground)/(3600*descr);
                remainNode.textContent = time.toFixed(2) + " hr descent remaining"
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
function toTimestamp(year,month,day,hour,minute){
    // Create date in local time, then convert to UTC timestamp
    // This allows users to enter local time and it gets converted to UTC for the simulation
    var datum = new Date(year,month-1,day,hour,minute);
    return datum.getTime()/1000;
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

function habmcshoweach(data2){
    let datetime = data2["Human Time"];
    var res = (datetime.substring(0,11)).split("-");
    var res2 = (datetime.substring(11,20)).split(":");
    var hourutc = parseInt(res2[0]) + 7;// Fix this for daylight savings...
    if(hourutc >= 24){
        document.getElementById("hr").value = hourutc - 24;
        const hrMobile = document.getElementById("hr-mobile");
        if(hrMobile) hrMobile.value = hourutc - 24;
        document.getElementById("day").value = parseInt(res[2]) + 1;
    }
    else{
        document.getElementById("hr").value = hourutc;
        const hrMobile = document.getElementById("hr-mobile");
        if(hrMobile) hrMobile.value = hourutc;
        document.getElementById("day").value = parseInt(res[2]);
    }
    document.getElementById("mn").value = parseInt(res2[1]);
    const mnMobile = document.getElementById("mn-mobile");
    if(mnMobile) mnMobile.value = parseInt(res2[1]);

    console.log(res2);

    document.getElementById("yr").value = parseInt(res[0]);
    document.getElementById("mo").value = parseInt(res[1]);
    document.getElementById("lat").value = lat = parseFloat(data2["latitude"]);
    document.getElementById("lon").value = lon = parseFloat(data2["longitude"]);
    position = {
        lat: lat,
        lng: lon,
    };
    var circle = new google.maps.Circle({
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
    //var formattedTime = hours + ':' + minutes.substr(-2) + ':' + seconds.substr(-2);
    var infowindow = new google.maps.InfoWindow({
        content: "Altitude: " + data2["altitude_gps"] + " Ground speed: " + data2["groundSpeed"] + data2["direction"] + " Ascent rate " + data2["ascentRate"]
    });

    //{"Human Time":"2019-10-05 14:47:14 -0700","transmit_time":1570312034000,"internal_temp":"-18.6","pressure":"  9632","altitude_barometer":"16002","latitude":"  37.082","longitude":"-119.419","altitude_gps":"16892","ballast_time":"0","vent_time":"0","iridium_latitude":"37.1840","iridium_longitude":"-119.5492","iridium_cep":5.0,"imei":"300234067160720","momsn":"93","id":19710,"updated_at":"2019-10-05 14:47:18 -0700","flightTime":15134,"batteryPercent":"NaN","ballastRemaining":0.0,"ballastPercent":"NaN","filtered_iridium_lat":36.911748,"filtered_iridium_lon":-120.754041,"raw_data":"2d31382e362c2020393633322c31363030322c202033372e3038322c2d3131392e3431392c31363839322c302c30","mission":66,"ascentRate":-0.03,"groundSpeed":8.88,"direction":"NORTH-EAST"}

    circle.addListener("mouseover", function () {
        infowindow.setPosition(circle.getCenter());
        infowindow.open(map);
    });
    circle.addListener("mouseout", function () {
        infowindow.close(map);
    });
    map.panTo(new google.maps.LatLng(lat, lon));

    alt = parseFloat(data2["altitude_gps"]);
    document.getElementById("alt").value = alt;
    rate = parseFloat(data2["ascentRate"]);
    console.log(rate)
    if(rate > 0){
        document.getElementById("asc").value = rate;
    }
    else {
        // This order matters because on standard profile there is no eqtime
        document.getElementById("equil").value = alt;
        document.getElementById("desc").value = -rate;
        document.getElementById("eqtime").value = 0;
    }
}
