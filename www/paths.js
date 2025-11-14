/**
 * Path rendering and trajectory visualization module.
 * 
 * Handles:
 * - Rendering trajectory paths on Google Maps
 * - Managing end pin markers and info windows
 * - Waypoint visualization
 * - Heatmap and contour rendering
 * - Ensemble and Monte Carlo simulation coordination
 */

// ============================================================================
// GLOBAL STATE VARIABLES
// ============================================================================

/** Cached trajectory path data (raw coordinate arrays) */
rawpathcache = [];
rawpathcacheModels = [];
var endPinMarker = null;
var endPinInfoWindow = null;
var endPinMarkers = [];
var endPinInfoWindows = [];
var waypointInfoWindows = [];
var lastLaunchInfo = null;
var multiConnectorPath = null;
var multiEndPositions = [];
var endPinZoomListener = null;
/** Array of waypoint circle markers for cleanup */
var circleslist = [];
/** Array of trajectory path polylines for cleanup */
var currpaths = [];
/** Array of contour layer objects (polygons and labels) */
var contourLayers = [];
/** Array of contour label markers */
var contourLabels = [];
/** Array of input values for validation (used in simulate function) */
var allValues = [];

// ============================================================================
// HELPER FUNCTIONS
// ============================================================================

/**
 * Format Unix timestamp (seconds) to HH:MM:SS string.
 * 
 * @param {number} timestamp - Unix timestamp in seconds
 * @returns {string} Formatted time string (e.g., "14:30:45")
 */
function formatTimeFromTimestamp(timestamp) {
    var date = new Date(timestamp * 1000);  // Convert seconds to milliseconds
    var hours = date.getHours();
    var minutes = "0" + date.getMinutes();
    var seconds = "0" + date.getSeconds();
    return hours + ':' + minutes.substr(-2) + ':' + seconds.substr(-2);
}

/**
 * Get timezone abbreviation for current locale (e.g., "PST", "EST", "UTC").
 * 
 * Uses Intl API to detect user's timezone. Falls back to UTC if detection fails.
 * 
 * @returns {string} Timezone abbreviation
 */
function getTimezoneAbbr() {
    try {
        var timeZone = Intl.DateTimeFormat().resolvedOptions().timeZone;
        var now = new Date();
        var formatter = new Intl.DateTimeFormat('en-US', {
            timeZone: timeZone,
            timeZoneName: 'short'
        });
        var parts = formatter.formatToParts(now);
        var tzPart = parts.find(function(part) { return part.type === 'timeZoneName'; });
        return (tzPart && tzPart.value) || 'UTC';
    } catch (e) {
        return 'UTC';  // Fallback to UTC if detection fails
    }
}

/**
 * Validate that a value is a positive number.
 * 
 * @param {string} value - Value to validate
 * @param {string} fieldName - Name of field for error message
 * @throws {Error} If value is empty, not a number, or <= 0
 */
function validatePositiveNumber(value, fieldName) {
    if (!value || value === "" || parseFloat(value) <= 0) {
        alert(fieldName + " must be a positive number");
        throw new Error("Invalid " + fieldName.toLowerCase());
    }
}

/**
 * Validate that a value is a non-negative number.
 * 
 * @param {string} value - Value to validate
 * @param {string} fieldName - Name of field for error message
 * @throws {Error} If value is empty, not a number, or < 0
 */
function validateNonNegativeNumber(value, fieldName) {
    if (!value || value === "" || parseFloat(value) < 0) {
        alert(fieldName + " must be a non-negative number");
        throw new Error("Invalid " + fieldName.toLowerCase());
    }
}

// ============================================================================
// MARKER AND VISUALIZATION FUNCTIONS
// ============================================================================

/**
 * Update end pin marker visibility and scale based on map zoom level.
 * 
 * Hides pins when zoomed out too far (zoom < 7) and scales them with zoom level
 * for better visibility. Called automatically when map zoom changes.
 * 
 * Dependencies: Requires global map object and endPinMarker/endPinMarkers arrays
 */
function updateEndPinVisibility() {
    try {
        if (!map) return;
        const zoom = map.getZoom ? map.getZoom() : 10;
        
        // Hide pins when zoomed out too far; scale pins with zoom
        const minZoomToShow = 7;
        const visible = zoom >= minZoomToShow;
        
        // Scaling relative to zoom (base scale is 7.5)
        // Formula: scale = (zoom - 6) * 1.0 + 5.5
        // Results: zoom 7 ~5.5, zoom 10 ~8.5, zoom 12 ~10.5
        let scale = Math.max(0, (zoom - 6) * 1.0 + 5.5);
        if (!visible) scale = 0;

        /**
         * Helper: Update visibility and scale for a single marker
         * @param {google.maps.Marker} marker - Marker to update
         */
        function setMarkerScale(marker) {
            if (!marker) return;
            if (typeof marker.setVisible === 'function') marker.setVisible(visible);
            const icon = marker.getIcon && marker.getIcon();
            if (icon && typeof icon === 'object') {
                const newIcon = Object.assign({}, icon, { scale: Math.max(5.0, Math.min(11.0, scale)) });
                // If hidden, set very small scale to avoid flashes
                if (!visible) newIcon.scale = 0.01;
                try { marker.setIcon(newIcon); } catch (e) {}
            }
        }

        // Update single mode marker
        if (endPinMarker) setMarkerScale(endPinMarker);
        
        // Update multi markers
        if (endPinMarkers && endPinMarkers.length) {
            for (const m of endPinMarkers) setMarkerScale(m);
        }
    } catch (e) {}
}

/**
 * Ensure zoom change listener is attached to map for end pin visibility updates.
 * 
 * Only creates listener if it doesn't already exist (prevents duplicate listeners).
 * Listener automatically calls updateEndPinVisibility() when map zoom changes.
 * 
 * Dependencies: Requires global map object and Google Maps API
 */
function ensureEndPinZoomListener() {
    try {
        if (!map || !google || !google.maps) return;
        if (endPinZoomListener) return; // Already attached
        endPinZoomListener = google.maps.event.addListener(map, 'zoom_changed', updateEndPinVisibility);
    } catch (e) {}
}

/**
 * Remove zoom change listener from map.
 * 
 * Called during cleanup to prevent memory leaks. Safe to call even if
 * listener doesn't exist.
 * 
 * Dependencies: Requires Google Maps API
 */
function clearEndPinZoomListener() {
    try {
        if (endPinZoomListener && google && google.maps && google.maps.event) {
            google.maps.event.removeListener(endPinZoomListener);
            endPinZoomListener = null;
        }
    } catch (e) {}
}

/**
 * Render trajectory paths on the map as polylines.
 * 
 * Converts raw path data (arrays of [time, lat, lon, alt]) into Google Maps
 * Polyline objects and adds them to the map. Control model (gec00) gets
 * thicker, more opaque lines for visibility.
 * 
 * @param {string} btype - Balloon type (STANDARD only)
 * @param {Array} allpaths - Array of path arrays, each containing [time, lat, lon, alt] points
 * @param {boolean} isControl - If true, render as control model (thicker, more opaque line)
 * 
 * Side effects:
 * - Adds paths to rawpathcache for later reference
 * - Adds polylines to currpaths array for cleanup
 * - Renders polylines on global map object
 */
function makepaths(btype, allpaths, isControl = false) {
    // Validate inputs
    if (!map) {
        console.warn('makepaths: map not initialized');
        return;
    }
    if (!allpaths || !Array.isArray(allpaths)) {
        console.warn('makepaths: invalid allpaths', allpaths);
        return;
    }
    
    // Cache raw path data for waypoint visualization
    rawpathcache.push(allpaths);
    
    // Convert each path array to Google Maps Polyline
    for (index in allpaths) {
        if (!allpaths[index] || !Array.isArray(allpaths[index]) || allpaths[index].length === 0) {
            continue; // Skip empty path segments
        }
        
        var pathpoints = [];

        // Convert [time, lat, lon, alt] points to {lat, lng} objects for Google Maps
        for (point in allpaths[index]) {
            var pointData = allpaths[index][point];
            if (!pointData || !Array.isArray(pointData) || pointData.length < 3) {
                continue; // Skip invalid points
            }
            var position = {
                lat: pointData[1],
                lng: pointData[2],
            };
            pathpoints.push(position);
        }
        
        // Skip if no valid points were found
        if (pathpoints.length === 0) {
            continue;
        }
        
        // Control model (gec00) gets thicker, solid line; perturbed models get thinner lines
        var drawpath = new google.maps.Polyline({
            path: pathpoints,
            geodesic: true,  // Follows Earth's curvature for long paths
            strokeColor: getcolor(index),  // Color based on model index
            strokeOpacity: isControl ? 1.0 : 0.7,  // Control: fully opaque, others: semi-transparent
            strokeWeight: isControl ? 4.5 : 2  // Control: thicker line
        });
        drawpath.setMap(map);
        currpaths.push(drawpath);  // Store for cleanup
    }
}
/**
 * Clear all visualizations from the map (paths, markers, heatmap, contours).
 * 
 * Comprehensive cleanup function called before new simulations to prevent
 * visual clutter. Safely handles missing or partially initialized objects.
 * 
 * Side effects:
 * - Removes all polylines, markers, info windows, heatmap, and contours
 * - Clears cached path data
 * - Resets global visualization state
 */
function clearAllVisualizations() {
    // Clear waypoints (circles and info windows)
    clearWaypoints();
    
    // Clear end pin markers and info windows
    clearEndPin();
    
    // Remove zoom listener (will be reattached when new end pins are created)
    clearEndPinZoomListener();
    
    // Clear multi mode connector path
    try {
        if (multiConnectorPath) {
            multiConnectorPath.setMap(null);
            multiConnectorPath = null;
        }
        multiEndPositions = [];
    } catch (e) {}
    
    // Remove all trajectory path polylines
    for (let path in currpaths) {
        if (currpaths[path]?.setMap) {
            currpaths[path].setMap(null);
        }
    }
    currpaths = [];
    
    // Clear heatmap layer (Monte Carlo visualization)
    if (heatmapLayer) {
        try {
            if (heatmapLayer.setMap) heatmapLayer.setMap(null);
            if (heatmapLayer.onRemove) heatmapLayer.onRemove();
            if (heatmapLayer._boundsListener) {
                google.maps.event.removeListener(heatmapLayer._boundsListener);
            }
        } catch (e) {
            console.warn('Error clearing heatmap:', e);
        }
        heatmapLayer = null;
    }
    
    // Clear contour lines (probability visualization)
    clearContours();
    
    // Clear cached path data
    rawpathcache = [];
    rawpathcacheModels = [];
}

/**
 * Clear all waypoint markers and their info windows from the map.
 * 
 * Waypoints are circular markers shown along trajectory paths when waypoint
 * toggle is enabled. This function removes them and closes associated info windows.
 * 
 * Side effects:
 * - Closes all waypoint info windows
 * - Removes all waypoint circles from map
 * - Clears waypointInfoWindows and circleslist arrays
 */
function clearWaypoints() {
    // Close all waypoint info windows
    waypointInfoWindows.forEach(win => {
        try {
            if (win?.getMap()) win.close();
        } catch (e) {}
    });
    waypointInfoWindows = [];
    
    // Remove all waypoint circles from map
    circleslist.forEach(circle => circle.setMap(null));
    circleslist = [];
}

/**
 * Display waypoint markers along trajectory paths.
 * 
 * Creates circular markers at each point along trajectory paths with info windows
 * showing time, location, and altitude. Skips the last point of each path (which
 * is shown as an end pin instead).
 * 
 * Dependencies:
 * - Requires rawpathcache to contain path data
 * - Requires waypointsToggle to be true
 * - Requires global map object
 * 
 * Side effects:
 * - Creates Google Maps Circle objects and adds them to map
 * - Creates InfoWindow objects for hover display
 * - Populates circleslist and waypointInfoWindows arrays
 */
function showWaypoints() {
    for (i in rawpathcache) {
        allpaths = rawpathcache[i]
        var modelIdForEntry = rawpathcacheModels[i];
        for (index in allpaths) {
            for (point in allpaths[index]){
                // Use IIFE to capture loop variables (prevents closure issues)
                (function () {
                    var position = {
                        lat: allpaths[index][point][1],
                        lng: allpaths[index][point][2],
                    };
                    if(waypointsToggle){
                        // Determine if this is the last point overall for this model's path
                        var isLastPointForThisModel = false;
                        try {
                            var segments = allpaths;
                            var lastSegIdx = -1;
                            for (var si = segments.length - 1; si >= 0; si--) {
                                if (segments[si] && segments[si].length && segments[si].length > 0) {
                                    lastSegIdx = si;
                                    break;
                                }
                            }
                            if (lastSegIdx >= 0) {
                                var lastPtIdx = segments[lastSegIdx].length - 1;
                                if (String(index) === String(lastSegIdx) && Number(point) === lastPtIdx) {
                                    isLastPointForThisModel = true;
                                }
                            }
                        } catch (e) {}
                        // Skip drawing the last waypoint circle when it's the end pin candidate:
                        // - always in single model mode
                        // - only for control model (modelId 0) in ensemble mode
                        var shouldSkipForEndPin = false;
                        try {
                            var isEnsemble = Array.isArray(window.availableModels) && window.availableModels.length > 1 && (window.ensembleEnabled || false);
                            if (isLastPointForThisModel) {
                                if (!isEnsemble) {
                                    shouldSkipForEndPin = true;
                                } else if (modelIdForEntry === 0) {
                                    shouldSkipForEndPin = true;
                                }
                            }
                        } catch (e) {}
                        if (shouldSkipForEndPin) {
                            return;
                        }
                        var circle = new google.maps.Circle({
                            strokeColor: getcolor(index),
                            strokeOpacity: 0.8,
                            strokeWeight: 2,
                            fillColor: getcolor(index),
                            fillOpacity: 0.35,
                            map: map,
                            center: position,
                            radius: 300,
                            clickable: true,
                            zIndex: 1000  // Higher z-index so waypoints appear above heatmap
                        });
                        circleslist.push(circle);
                        // Format timestamp to HH:MM:SS using helper function
                        var formattedTime = formatTimeFromTimestamp(allpaths[index][point][0]);
                        
                        // Get timezone abbreviation using helper function
                        var tzAbbr = getTimezoneAbbr();
                        
                        // Round altitude to 2 decimal places
                        var altitude = parseFloat(allpaths[index][point][3]);
                        var roundedAltitude = isNaN(altitude) ? allpaths[index][point][3] : altitude.toFixed(2);
                        
                        // Round latitude and longitude to 5 decimal places (~1m precision)
                        var lat = parseFloat(allpaths[index][point][1]);
                        var lon = parseFloat(allpaths[index][point][2]);
                        var roundedLat = isNaN(lat) ? allpaths[index][point][1] : lat.toFixed(5);
                        var roundedLon = isNaN(lon) ? allpaths[index][point][2] : lon.toFixed(5);
                        
                        var infowindow = new google.maps.InfoWindow({
                            content: '<div style="font-family: -apple-system, BlinkMacSystemFont, \'Segoe UI\', Roboto, \'Helvetica Neue\', Arial, sans-serif; padding: 4px 6px; line-height: 1.5; color: #000000;">' +
                                     '<div style="margin-bottom: 4px;"><strong style="font-weight: 600;">Wayp. Lat:</strong> ' + roundedLat + '°</div>' +
                                     '<div style="margin-bottom: 4px;"><strong style="font-weight: 600;">Wayp. Lon:</strong> ' + roundedLon + '°</div>' +
                                     '<div style="margin-bottom: 4px;"><strong style="font-weight: 600;">Wayp. Altitude:</strong> ' + roundedAltitude + ' m</div>' +
                                     '<div><strong style="font-weight: 600;">Wayp. Time:</strong> ' + formattedTime + ' ' + tzAbbr + '</div>' +
                                     '</div>'
                        });
                        // Store info window so we can close it when toggling off waypoints
                        waypointInfoWindows.push(infowindow);
                        circle.addListener("mouseover", function () {
                            infowindow.setPosition(circle.getCenter());
                            infowindow.open(map);
                        });
                        circle.addListener("mouseout", function () {
                            infowindow.close(map);
                        });
                    }
                }());
            }
        }
    }
}

/**
 * Close end pin info windows without removing markers.
 * 
 * Used for mobile tap-outside behavior where info windows should close
 * but markers should remain visible. Different from clearEndPin() which
 * removes markers entirely.
 * 
 * Side effects: Closes info windows but preserves markers
 */
function closeEndPinInfoWindows() {
    try {
        // Close single mode info window
        if (endPinInfoWindow && endPinInfoWindow.getMap()) {
            endPinInfoWindow.close();
        }
        // Close multi mode info windows
        if (endPinInfoWindows && endPinInfoWindows.length) {
            endPinInfoWindows.forEach(iw => {
                try {
                    if (iw && iw.getMap()) {
                        iw.close();
                    }
                } catch(e) {}
            });
        }
    } catch (e) {}
}

/**
 * Clear all end pin markers and info windows from the map.
 * 
 * Removes both single mode and multi mode end pin markers, closes their
 * info windows, and resets related state. Called during cleanup before
 * new simulations.
 * 
 * Side effects:
 * - Removes markers from map
 * - Closes and clears info windows
 * - Resets endPinMarker, endPinMarkers, endPinInfoWindows arrays
 * - Clears multiEndPositions array
 */
function clearEndPin() {
    try {
        // Clear single mode marker and info window
        if (endPinInfoWindow) { endPinInfoWindow.close(); endPinInfoWindow = null; }
        if (endPinMarker) { endPinMarker.setMap(null); endPinMarker = null; }
        
        // Clear multi mode markers and info windows
        if (endPinInfoWindows && endPinInfoWindows.length) {
            endPinInfoWindows.forEach(iw => { try { iw.close(); } catch(e){} });
        }
        if (endPinMarkers && endPinMarkers.length) {
            endPinMarkers.forEach(m => { try { m.setMap(null); } catch(e){} });
        }
        
        // Reset arrays
        endPinInfoWindows = [];
        endPinMarkers = [];
        multiEndPositions = [];
    } catch (e) {}
}

/**
 * Create and display an end pin marker at the landing location.
 * 
 * End pins show where the balloon lands. In single mode, only one pin is shown.
 * In multi mode, multiple pins are shown (one per time offset). Pins are clickable
 * and show detailed landing information in an info window.
 * 
 * @param {Array} endPoint - Landing point [time, lat, lon, alt] where time is Unix timestamp in seconds
 * @param {string} color - Hex color for the pin marker (e.g., "#FF0000")
 * @param {number} hourOffset - Optional hour offset for multi mode (adds to launch time for display)
 * 
 * Side effects:
 * - Creates Google Maps Marker and adds to map
 * - Creates InfoWindow for click interaction
 * - Updates endPinMarker (single mode) or endPinMarkers array (multi mode)
 * - Attaches zoom listener if not already attached
 */
function setEndPin(endPoint, color, hourOffset) {
    try {
        // In Multi mode, we keep all end pins; otherwise clear previous
        if (!window.multiActive) {
            clearEndPin();
        }
        var position = new google.maps.LatLng(endPoint[1], endPoint[2]);
        // Circle icon - determine outline color based on mode
        var outlineColor = "#000000"; // Default black for normal/ensemble
        if (window.multiActive && typeof hourOffset === 'number') {
            // In multi mode: black outline for every 24 hours (0, 24, 48, 72, 96, 120, 144, 168), white for others
            if (hourOffset % 24 === 0) {
                outlineColor = "#000000"; // Black for 0, 24, 48, 72, 96, 120, 144, 168
            } else {
                outlineColor = "#ffffff"; // White for other times
            }
        }
        var icon = {
            path: google.maps.SymbolPath.CIRCLE,
            fillColor: color || "#000000",
            fillOpacity: 0.9,
            strokeColor: outlineColor,
            strokeWeight: 2,
            scale: 7.5
        };
        var marker = new google.maps.Marker({
            position: position,
            map: map,
            icon: icon,
            zIndex: 1500,
            optimized: false  // Better click detection
        });
        // Build info content on click (no hover)
        // Format landing time using helper function
        var formattedTime = formatTimeFromTimestamp(endPoint[0]);
        var tzAbbr = getTimezoneAbbr();
        var altitude = parseFloat(endPoint[3]);
        var roundedAltitude = isNaN(altitude) ? endPoint[3] : altitude.toFixed(2);
        var lat = parseFloat(endPoint[1]);
        var lon = parseFloat(endPoint[2]);
        var roundedLat = isNaN(lat) ? endPoint[1] : lat.toFixed(5);
        var roundedLon = isNaN(lon) ? endPoint[2] : lon.toFixed(5);
        var launchSection = '';
        if (lastLaunchInfo && typeof lastLaunchInfo.time === 'number') {
            // Adjust launch time by hour offset if in multi mode
            var launchTime = lastLaunchInfo.time;
            if (typeof hourOffset === 'number' && hourOffset !== 0) {
                launchTime = launchTime + (hourOffset * 3600);
            }
            // Format launch date and time
            var ldate = new Date(launchTime * 1000);
            var lyear = ldate.getFullYear();
            var lmonth = "0" + (ldate.getMonth() + 1);
            var lday = "0" + ldate.getDate();
            var lformattedDate = lyear + '-' + lmonth.substr(-2) + '-' + lday.substr(-2);
            var lformattedTime = formatTimeFromTimestamp(launchTime);
            var llat = parseFloat(lastLaunchInfo.lat);
            var llon = parseFloat(lastLaunchInfo.lon);
            var lroundedLat = isNaN(llat) ? lastLaunchInfo.lat : llat.toFixed(5);
            var lroundedLon = isNaN(llon) ? lastLaunchInfo.lon : llon.toFixed(5);
            launchSection = ''
                + '<div style="margin-top: 6px; padding-top: 6px; border-top: 1px solid #eee;">'
                + '<div style="margin-bottom: 4px;"><strong style="font-weight: 600;">Launch Lat:</strong> ' + lroundedLat + '°</div>'
                + '<div style="margin-bottom: 4px;"><strong style="font-weight: 600;">Launch Lon:</strong> ' + lroundedLon + '°</div>'
                + '<div style="margin-bottom: 4px;"><strong style="font-weight: 600;">Launch Date:</strong> ' + lformattedDate + '</div>'
                + '<div><strong style="font-weight: 600;">Launch Time:</strong> ' + lformattedTime + ' ' + tzAbbr + '</div>'
                + '</div>';
        }
        // Generate unique ID for this info window to enable X button functionality
        var infoId = 'endpin-info-' + Date.now() + '-' + Math.random().toString(36).substr(2, 9);
        
        var contentHtml = '<div style="font-family: -apple-system, BlinkMacSystemFont, \'Segoe UI\', Roboto, \'Helvetica Neue\', Arial, sans-serif; padding: 6px 8px; line-height: 1.5; position: relative;">'
            + '<button id="' + infoId + '-close" style="position: absolute; top: 4px; right: 4px; background: none; border: none; font-size: 18px; line-height: 1; cursor: pointer; color: #666; padding: 2px 6px; font-weight: bold;" title="Close">×</button>'
            + '<div style="margin-bottom: 4px;"><strong style="font-weight: 600;">Land Lat:</strong> ' + roundedLat + '°</div>'
            + '<div style="margin-bottom: 4px;"><strong style="font-weight: 600;">Land Lon:</strong> ' + roundedLon + '°</div>'
            + '<div style="margin-bottom: 4px;"><strong style="font-weight: 600;">Land Altitude:</strong> ' + roundedAltitude + 'm</div>'
            + '<div style="margin-bottom: 4px;"><strong style="font-weight: 600;">Land Time:</strong> ' + formattedTime + ' ' + tzAbbr + '</div>'
            + launchSection
            + '</div>';
        var info = new google.maps.InfoWindow({ content: contentHtml });
        
        // Function to close info window and (for multi mode) clear trajectory
        var closeInfoAndTrajectory = function() {
            info.close();
            // For multi mode, also clear trajectory if it exists
            if (window.multiActive && marker._polylines && marker._polylines.length) {
                try {
                    for (const pl of marker._polylines) {
                        try { pl.setMap(null); } catch (e) {}
                    }
                    currpaths = currpaths.filter(pl => marker._polylines.indexOf(pl) === -1);
                    if (marker._rawIndex !== null && marker._rawIndex >= 0) {
                        rawpathcache[marker._rawIndex] = [];
                        rawpathcacheModels[marker._rawIndex] = null;
                    }
                    marker._polylines = null;
                    marker._rawIndex = null;
                    if (typeof clearWaypoints === 'function') {
                        clearWaypoints();
                        if (waypointsToggle) { showWaypoints(); }
                    }
                } catch (e) {
                    console.warn('Error clearing trajectory on X click', e);
                }
            }
        };
        
        // Attach X button click handler after InfoWindow opens
        google.maps.event.addListener(info, 'domready', function() {
            var closeBtn = document.getElementById(infoId + '-close');
            if (closeBtn) {
                closeBtn.addEventListener('click', function(e) {
                    e.stopPropagation();
                    closeInfoAndTrajectory();
                });
                // Hover effect for X button
                closeBtn.addEventListener('mouseenter', function() {
                    closeBtn.style.color = '#000';
                });
                closeBtn.addEventListener('mouseleave', function() {
                    closeBtn.style.color = '#666';
                });
            }
        });
        
        marker.addListener('click', function() {
            // Toggle behavior: if already open, close it; otherwise open it
            if (info.getMap()) {
                // Info window is open, close it
                closeInfoAndTrajectory();
            } else {
                // Close any other open end pin info windows first
                if (window.multiActive) {
                    // Close all other multi end pin info windows
                    for (var i = 0; i < endPinInfoWindows.length; i++) {
                        if (endPinInfoWindows[i] && endPinInfoWindows[i].getMap()) {
                            endPinInfoWindows[i].close();
                        }
                    }
                } else {
                    // Close single end pin info window if open
                    if (endPinInfoWindow && endPinInfoWindow.getMap()) {
                        endPinInfoWindow.close();
                    }
                }
                // Open this info window
                info.open(map, marker);
            }
        });
        if (window.multiActive) {
            endPinMarkers.push(marker);
            endPinInfoWindows.push(info);
            // Track position for multi connector
            try { multiEndPositions.push(position); } catch (e) {}
        } else {
            endPinMarker = marker;
            endPinInfoWindow = info;
        }
        // Ensure zoom handling is active and apply current zoom state
        ensureEndPinZoomListener();
        updateEndPinVisibility();
    } catch (e) {
        console.warn('Failed to set end pin', e);
    }
    return marker;
}

function addMultiEndPin(payload, hourOffset, color) {
    try {
        // STANDARD mode: path[0] = rise, path[1] = empty (no equilibrium), path[2] = fall
        let rise = payload[0];
        let equil = [];
        let fall = payload[2];
        let fpath = [];
        const allpaths = [rise, equil, fall, fpath];
        // Determine end point
        let lastSegIdx = -1;
        for (let si = allpaths.length - 1; si >= 0; si--) {
            if (allpaths[si] && allpaths[si].length > 0) { lastSegIdx = si; break; }
        }
        if (lastSegIdx < 0) return;
        const endPoint = allpaths[lastSegIdx][allpaths[lastSegIdx].length - 1];
        // Place the end pin (returns marker)
        const marker = setEndPin(endPoint, color, hourOffset);
        if (!marker) return;
        // Attach toggle behavior for trajectory rendering
        marker._polylines = null;
        marker._rawIndex = null;
        marker.addListener('click', function() {
            try {
                if (marker._polylines && marker._polylines.length) {
                    // Remove existing trajectory
                    for (const pl of marker._polylines) {
                        try { pl.setMap(null); } catch (e) {}
                    }
                    // Remove from global currpaths
                    try {
                        currpaths = currpaths.filter(pl => marker._polylines.indexOf(pl) === -1);
                    } catch (e) {}
                    // Remove from rawpathcache to update waypoints
                    if (marker._rawIndex !== null && marker._rawIndex >= 0) {
                        try {
                            rawpathcache[marker._rawIndex] = [];
                            rawpathcacheModels[marker._rawIndex] = null;
                        } catch (e) {}
                    }
                    marker._polylines = null;
                    marker._rawIndex = null;
                    // Refresh waypoints
                    if (typeof clearWaypoints === 'function') {
                        clearWaypoints();
                        if (waypointsToggle) { showWaypoints(); }
                    }
                } else {
                    // Draw trajectory now
                    const prevLen = currpaths.length;
                    // Track model id for rawpathcache alignment; use model 0 for multi
                    try { rawpathcacheModels.push(0); } catch (e) {}
                    // Use isControl=true to make multi trajectories thicker (like normal simulate mode)
                    makepaths(btype, allpaths, true);
                    const newPolys = currpaths.slice(prevLen);
                    marker._polylines = newPolys;
                    marker._rawIndex = (rawpathcache && rawpathcache.length) ? rawpathcache.length - 1 : null;
                    // Refresh waypoints if enabled
                    if (waypointsToggle) {
                        if (typeof clearWaypoints === 'function') clearWaypoints();
                        if (typeof showWaypoints === 'function') showWaypoints();
                    }
                }
            } catch (e) {
                console.warn('Error toggling multi trajectory', e);
            }
        });
    } catch (e) {
        console.warn('Failed to add multi end pin', e);
    }
}

function showpath(path, modelId = 1, hourOffset = null, endpointColor = null) {
    // Validate path data
    if (!path || !Array.isArray(path)) {
        console.warn('showpath: invalid path data', path);
        return;
    }
    
    // STANDARD mode: path[0] = rise, path[1] = empty (no equilibrium), path[2] = fall
    var rise = path[0];
    var equil = [];
    var fall = path[2];
    var fpath = [];
    
    var allpaths = [rise, equil, fall, fpath];
    const isControl = (modelId === 0);
    // Track model id for this path entry to support end pin exclusion logic
    rawpathcacheModels.push(modelId);
    
    console.log(`showpath: modelId=${modelId}, rise points=${rise?.length || 0}, fall points=${fall?.length || 0}`);
    makepaths(btype, allpaths, isControl);

    // Determine the last point of this model's trajectory and set end pin when applicable
    try {
        var segments = allpaths;
        var lastSegIdx = -1;
        for (var si = segments.length - 1; si >= 0; si--) {
            if (segments[si] && segments[si].length && segments[si].length > 0) {
                lastSegIdx = si;
                break;
            }
        }
        if (lastSegIdx >= 0) {
            var endPoint = segments[lastSegIdx][segments[lastSegIdx].length - 1]; // [t, lat, lon, alt]
            var isEnsemble = Array.isArray(window.availableModels) && window.availableModels.length > 1 && (window.ensembleEnabled || false);
            if (!isEnsemble || modelId === 0) {
                // Set a single end pin: always for single; in ensemble only for control model
                setEndPin(endPoint, endpointColor || getcolor('0'), hourOffset);
            }
        }
    } catch (e) {
        console.warn('Could not determine end point for end pin', e);
    }
}

/**
 * Get color for trajectory path based on model index.
 * 
 * Returns distinct colors for different model indices. Used for rendering
 * trajectory paths and waypoint markers with model-specific colors.
 * 
 * @param {string|number} index - Model index ('0', '1', '2', '3', or numeric)
 * @returns {string} Hex color code
 */
function getcolor(index){
    // Convert to string for consistent comparison
    const idx = String(index);
    switch(idx){
        case '0':
            return '#DC143C';  // Crimson red (control model)
        case '1':
            return '#0000FF';  // Blue
        case '2':
            return '#000000';  // Black
        case '3':
            return '#000000';  // Black
        default:
            // Fallback to black for unknown indices
            return '#000000';
    }
}

// ============================================================================
// CUSTOM HEATMAP OVERLAY: Preserves actual data shape without circular smoothing
// ============================================================================
// Custom Google Maps OverlayView that renders heatmap using canvas with controllable
// smoothing. This avoids Google Maps' built-in Gaussian smoothing that creates circular
// patterns, allowing the actual data distribution shape to be preserved.
//
// Configuration options:
// - smoothingType: 'none' (raw density), 'epanechnikov' (epanechnikov kernel), 
//   'uniform' (uniform kernel), or 'gaussian' (custom Gaussian with configurable bandwidth)
// - smoothingBandwidth: controls the smoothing amount (in degrees)
// - opacity: overlay opacity (0-1)
// - gridResolution: density grid resolution (higher = smoother but slower)
// ============================================================================

// Lazy initialization: Define CustomHeatmapOverlay class only when needed
let CustomHeatmapOverlay = null;

function getCustomHeatmapOverlayClass() {
    // Return cached class if already defined
    if (CustomHeatmapOverlay) {
        return CustomHeatmapOverlay;
    }
    
    // Check if Google Maps is loaded
    if (typeof google === 'undefined' || !google.maps || !google.maps.OverlayView) {
        return null;
    }
    
    // Define the class once and cache it
    CustomHeatmapOverlay = class extends google.maps.OverlayView {
    constructor(points, options = {}) {
        super();
        this.points = points; // Array of {lat, lon} objects
        this.opacity = options.opacity || 0.6;
        this.smoothingType = options.smoothingType || 'epanechnikov'; // 'none', 'epanechnikov', 'uniform', 'gaussian'
        this.smoothingBandwidth = options.smoothingBandwidth || null; // null = auto-calculate
        this.gridResolution = options.gridResolution || 100; // 100x100 grid
        this.gradient = options.gradient || [
            {stop: 0.0, color: 'rgba(0, 255, 255, 0)'},      // Cyan (transparent) - low density
            {stop: 0.2, color: 'rgba(0, 255, 255, 0.5)'},    // Cyan - medium-low
            {stop: 0.4, color: 'rgba(0, 255, 0, 0.7)'},      // Green - medium
            {stop: 0.6, color: 'rgba(255, 255, 0, 0.8)'},    // Yellow - medium-high
            {stop: 0.8, color: 'rgba(255, 165, 0, 0.9)'},    // Orange - high
            {stop: 1.0, color: 'rgba(255, 0, 0, 1)'}         // Red - highest density
        ];
        
        this.canvas = null;
        this.densityGrid = null;
        this.bounds = null;
    }
    
    onAdd() {
        // Create canvas element
        this.canvas = document.createElement('canvas');
        this.canvas.style.position = 'absolute';
        this.canvas.style.opacity = this.opacity;
        this.canvas.style.pointerEvents = 'none';
        this.canvas.style.zIndex = '1';  // Lower z-index so waypoints appear above
        
        // Add canvas to panes
        const panes = this.getPanes();
        // Place heatmap below interactive overlays so waypoints remain hoverable
        if (panes.overlayImage) {
            panes.overlayImage.appendChild(this.canvas);
        } else {
            panes.overlayLayer.appendChild(this.canvas);
        }
    }
    
    onRemove() {
        if (this.canvas && this.canvas.parentNode) {
            this.canvas.parentNode.removeChild(this.canvas);
        }
        this.canvas = null;
    }
    
    draw() {
        if (!this.canvas || this.points.length === 0) return;
        
        const projection = this.getProjection();
        if (!projection) return;
        
        // Calculate bounds of visible area
        const mapBounds = map.getBounds();
        const ne = mapBounds.getNorthEast();
        const sw = mapBounds.getSouthWest();
        
        this.bounds = {
            minLat: sw.lat(),
            maxLat: ne.lat(),
            minLon: sw.lng(),
            maxLon: ne.lng()
        };
        
        // Get canvas size
        const div = this.getPanes().overlayLayer;
        this.canvas.width = div.offsetWidth;
        this.canvas.height = div.offsetHeight;
        
        // Calculate density grid
        this.calculateDensityGrid();
        
        // Render heatmap
        this.renderHeatmap(projection);
    }
    
    calculateDensityGrid() {
        const gridSize = this.gridResolution;
        const grid = Array(gridSize).fill(0).map(() => Array(gridSize).fill(0));
        
        const latStep = (this.bounds.maxLat - this.bounds.minLat) / gridSize;
        const lonStep = (this.bounds.maxLon - this.bounds.minLon) / gridSize;
        
        // Auto-calculate bandwidth if not provided
        let bandwidth = this.smoothingBandwidth;
        if (!bandwidth) {
            const latRange = this.bounds.maxLat - this.bounds.minLat;
            const lonRange = this.bounds.maxLon - this.bounds.minLon;
            const dataRange = Math.max(latRange, lonRange);
            // Use 5% of data range as default bandwidth (adjustable)
            bandwidth = dataRange * 0.05;
        }
        
        // Calculate density at each grid point
        for (let i = 0; i < gridSize; i++) {
            for (let j = 0; j < gridSize; j++) {
                const gridLat = this.bounds.minLat + (i + 0.5) * latStep;
                const gridLon = this.bounds.minLon + (j + 0.5) * lonStep;
                
                let density = 0;
                for (const point of this.points) {
                    const latDist = (point.lat - gridLat) / bandwidth;
                    const lonDist = (point.lon - gridLon) / bandwidth;
                    const distSq = latDist * latDist + lonDist * lonDist;
                    const dist = Math.sqrt(distSq);
                    
                    // Apply smoothing kernel
                    let kernelWeight = 0;
                    if (this.smoothingType === 'none') {
                        // No smoothing: raw point count (use small radius for binning)
                        if (distSq < (bandwidth * 0.1) ** 2) {
                            kernelWeight = 1;
                        }
                    } else if (this.smoothingType === 'epanechnikov') {
                        // Epanechnikov kernel (more rectangular, preserves shape better)
                        if (dist <= 1) {
                            kernelWeight = (1 - distSq) * 3 / 4; // Epanechnikov kernel
                        }
                    } else if (this.smoothingType === 'uniform') {
                        // Uniform kernel (rectangular)
                        if (dist <= 1) {
                            kernelWeight = 1;
                        }
                    } else if (this.smoothingType === 'gaussian') {
                        // Gaussian kernel (smooth but circular)
                        kernelWeight = Math.exp(-distSq / 2);
                    }
                    
                    // Apply point weight (ensemble points weighted more heavily than Monte Carlo)
                    const pointWeight = point.weight || 1.0;
                    density += kernelWeight * pointWeight;
                }
                grid[i][j] = density;
            }
        }
        
        this.densityGrid = grid;
    }
    
    renderHeatmap(projection) {
        const ctx = this.canvas.getContext('2d');
        ctx.clearRect(0, 0, this.canvas.width, this.canvas.height);
        
        if (!this.densityGrid) return;
        
        const gridSize = this.gridResolution;
        const maxDensity = Math.max(...this.densityGrid.flat());
        if (maxDensity === 0) return;
        
        const latStep = (this.bounds.maxLat - this.bounds.minLat) / gridSize;
        const lonStep = (this.bounds.maxLon - this.bounds.minLon) / gridSize;
        const pixelStep = Math.max(this.canvas.width / gridSize, this.canvas.height / gridSize);
        
        // Draw heatmap
        for (let i = 0; i < gridSize; i++) {
            for (let j = 0; j < gridSize; j++) {
                const density = this.densityGrid[i][j];
                if (density === 0) continue;
                
                const normalizedDensity = density / maxDensity;
                const color = this.getColorForDensity(normalizedDensity);
                
                const gridLat = this.bounds.minLat + (i + 0.5) * latStep;
                const gridLon = this.bounds.minLon + (j + 0.5) * lonStep;
                
                const point = new google.maps.LatLng(gridLat, gridLon);
                const pixel = projection.fromLatLngToDivPixel(point);
                
                ctx.fillStyle = color;
                ctx.fillRect(pixel.x - pixelStep/2, pixel.y - pixelStep/2, pixelStep, pixelStep);
            }
        }
    }
    
    getColorForDensity(density) {
        // Find the two gradient stops to interpolate between
        for (let i = 0; i < this.gradient.length - 1; i++) {
            const stop1 = this.gradient[i];
            const stop2 = this.gradient[i + 1];
            
            if (density >= stop1.stop && density <= stop2.stop) {
                const t = (density - stop1.stop) / (stop2.stop - stop1.stop);
                return this.interpolateColor(stop1.color, stop2.color, t);
            }
        }
        
        // Fallback to last color
        return this.gradient[this.gradient.length - 1].color;
    }
    
    interpolateColor(color1, color2, t) {
        // Parse rgba strings
        const parseRGBA = (colorStr) => {
            const match = colorStr.match(/rgba?\((\d+),\s*(\d+),\s*(\d+)(?:,\s*([\d.]+))?\)/);
            if (match) {
                return {
                    r: parseInt(match[1]),
                    g: parseInt(match[2]),
                    b: parseInt(match[3]),
                    a: match[4] ? parseFloat(match[4]) : 1
                };
            }
            return {r: 0, g: 0, b: 0, a: 1};
        };
        
        const c1 = parseRGBA(color1);
        const c2 = parseRGBA(color2);
        
        const r = Math.round(c1.r + (c2.r - c1.r) * t);
        const g = Math.round(c1.g + (c2.g - c1.g) * t);
        const b = Math.round(c1.b + (c2.b - c1.b) * t);
        const a = c1.a + (c2.a - c1.a) * t;
        
        return `rgba(${r}, ${g}, ${b}, ${a})`;
    }
    };
    
    return CustomHeatmapOverlay;
}

function displayHeatmap(heatmapData) {
    try {
        // Check if Google Maps API is loaded
        if (!window.google || !window.google.maps) {
            console.error('Google Maps API not loaded yet. Waiting...');
            setTimeout(() => displayHeatmap(heatmapData), 1000);
            return;
        }
        
        // Get CustomHeatmapOverlay class (lazy initialization)
        const HeatmapClass = getCustomHeatmapOverlayClass();
        if (!HeatmapClass) {
            console.error('CustomHeatmapOverlay not available. Waiting for Google Maps...');
            setTimeout(() => displayHeatmap(heatmapData), 500);
            return;
        }
        
        // Check if map is initialized
        if (!map) {
            console.error('Map not initialized yet');
            setTimeout(() => displayHeatmap(heatmapData), 500);
            return;
        }
        
        // Clear existing heatmap and contours if any (prevents overlapping visualizations)
        if (heatmapLayer) {
            try {
                if (heatmapLayer.setMap) {
            heatmapLayer.setMap(null);
                }
                if (heatmapLayer.onRemove) {
                    heatmapLayer.onRemove();
                }
                // Also remove any event listeners
                if (heatmapLayer._boundsListener) {
                    google.maps.event.removeListener(heatmapLayer._boundsListener);
                }
            } catch (e) {
                console.warn('Error clearing heatmap:', e);
            }
            heatmapLayer = null;
        }
        clearContours();  // Clear existing contours
        
        if (!heatmapData || heatmapData.length === 0) {
            console.log('No heatmap data to display');
            return;
        }
        
        console.log(`Creating custom heatmap with ${heatmapData.length} landing positions`);
        
        // Convert landing positions to normalized coordinates
        // Preserve weight field for weighted density calculation
        const heatmapPoints = heatmapData.map(point => {
            // Validate point has lat/lon
            if (typeof point.lat !== 'number' || typeof point.lon !== 'number') {
                console.warn('Invalid heatmap point:', point);
                return null;
            }
            // Normalize longitude to [-180, 180] for display
            let lon = point.lon;
            if (lon > 180) {
                lon = ((lon + 180) % 360) - 180;
            }
            // Preserve weight field (default to 1.0 if not specified)
            const weight = typeof point.weight === 'number' ? point.weight : 1.0;
            return { lat: point.lat, lon: lon, weight: weight };
        }).filter(p => p !== null); // Remove invalid points
        
        if (heatmapPoints.length === 0) {
            console.warn('No valid heatmap points after filtering');
            return;
        }
        
        console.log(`Creating contours with ${heatmapPoints.length} valid points`);
        
        // Skip heatmap - only display contours with colored shading
        // Generate and display probability contours with labels
        displayContours(heatmapData);
    } catch (error) {
        console.error('Error displaying heatmap:', error);
        console.error('Heatmap data:', heatmapData);
    }
}

// ============================================================================
// CONTOUR GENERATION: Probability Contours with Labels
// ============================================================================
// Generates probability density contours from Monte Carlo landing positions
// and displays them as labeled contour lines on the map.
//
// Process:
// 1. Create density grid using kernel density estimation (KDE)
// 2. Extract contour lines at probability thresholds (10%, 30%, 50%, 70%, 90%)
// 3. Draw contours as polylines with appropriate colors
// 4. Add text labels at strategic positions along contours
// 5. Implement zoom-based visibility (hide when zoomed out too far)
// ============================================================================

function clearContours() {
    // Remove all contour polylines and labels
    if (contourLayers && contourLayers.length > 0) {
    contourLayers.forEach(layer => {
            try {
                // Remove polyline1 from map - use multiple methods to ensure it's removed
                if (layer.polyline) {
                    try {
                        // Method 1: Remove from map
                        if (layer.polyline.setMap) {
                            layer.polyline.setMap(null);
                        }
                        // Method 2: Clear path to remove all points
                        if (typeof layer.polyline.setPath === 'function') {
                            layer.polyline.setPath([]);
                        }
                        // Method 3: Set visibility to false
                        if (typeof layer.polyline.setVisible === 'function') {
                            layer.polyline.setVisible(false);
                        }
                        // Method 4: Set opacity to 0
                        if (typeof layer.polyline.setOptions === 'function') {
                            layer.polyline.setOptions({ strokeOpacity: 0, visible: false });
                        }
                    } catch (e) {
                        console.warn('Error removing polyline:', e);
                    }
                }
                
                // Remove polyline2 from map - use multiple methods to ensure it's removed
                if (layer.polyline2) {
                    try {
                        // Method 1: Remove from map
                        if (layer.polyline2.setMap) {
                            layer.polyline2.setMap(null);
                        }
                        // Method 2: Clear path to remove all points
                        if (typeof layer.polyline2.setPath === 'function') {
                            layer.polyline2.setPath([]);
                        }
                        // Method 3: Set visibility to false
                        if (typeof layer.polyline2.setVisible === 'function') {
                            layer.polyline2.setVisible(false);
                        }
                        // Method 4: Set opacity to 0
                        if (typeof layer.polyline2.setOptions === 'function') {
                            layer.polyline2.setOptions({ strokeOpacity: 0, visible: false });
                        }
                    } catch (e) {
                        console.warn('Error removing polyline2:', e);
                    }
                }
                
                // Remove label from map
                if (layer.label) {
                    if (layer.label.setMap) {
                        layer.label.setMap(null);
                    }
                    // Try to delete the object
                    try {
                        delete layer.label;
                    } catch (e) {}
                }
            } catch (e) {
                console.warn('Error clearing contour layer:', e);
            }
        });
    }
    
    // Clear arrays immediately
    contourLayers = [];
    contourLabels = [];
    
    // Also remove zoom listener that was added for contours
    if (map && map._contourZoomListener && google && google.maps && google.maps.event) {
        try {
            google.maps.event.removeListener(map._contourZoomListener);
            map._contourZoomListener = null;
        } catch (e) {
            // Ignore errors if listener doesn't exist
        }
    }
    
    console.log("Cleared all contours");
}

function displayContours(heatmapData) {
    try {
        if (!heatmapData || heatmapData.length === 0) return;
        if (!map) return;
        
        // Normalize and validate points, preserve weight field
        const points = heatmapData.map(point => {
            if (typeof point.lat !== 'number' || typeof point.lon !== 'number') return null;
            let lon = point.lon;
            if (lon > 180) lon = ((lon + 180) % 360) - 180;
            const weight = typeof point.weight === 'number' ? point.weight : 1.0;
            return { lat: point.lat, lon: lon, weight: weight };
        }).filter(p => p !== null);
        
        if (points.length < 10) {
            console.log('Not enough points for contour generation');
            return;
        }
        
        // Calculate bounding box of landing positions
        const lats = points.map(p => p.lat);
        const lons = points.map(p => p.lon);
        const minLat = Math.min(...lats);
        const maxLat = Math.max(...lats);
        const minLon = Math.min(...lons);
        const maxLon = Math.max(...lons);
        
        // Add padding to bounding box
        const latRange = maxLat - minLat;
        const lonRange = maxLon - minLon;
        const padding = Math.max(latRange, lonRange) * 0.2;  // 20% padding
        
        const bounds = {
            minLat: minLat - padding,
            maxLat: maxLat + padding,
            minLon: minLon - padding,
            maxLon: maxLon + padding
        };
        
        // Create density grid using kernel density estimation
        const gridSize = 50;  // 50x50 grid for density calculation
        const densityGrid = createDensityGrid(points, bounds, gridSize);
        
        // Extract contours at cumulative probability thresholds
        // Compute density cutoff values so that area ABOVE cutoff contains target mass
        const thresholds = [0.3, 0.5, 0.7, 0.9];  // 30%, 50%, 70%, 90% (higher % encloses larger area)
        const flat = densityGrid.flat();
        const totalMass = flat.reduce((a, b) => a + b, 0);
        const sortedDesc = [...flat].sort((a, b) => b - a);

        function cutoffForMass(targetMassFraction) {
            const target = totalMass * targetMassFraction;
            let acc = 0;
            for (let i = 0; i < sortedDesc.length; i++) {
                acc += sortedDesc[i];
                if (acc >= target) {
                    return sortedDesc[i];
                }
            }
            return 0;
        }
        
        // Draw from outermost (90%) to innermost (30%) so inner colors are visible
        thresholds.slice().reverse().forEach((threshold, indexFromOuter) => {
            const densityValue = cutoffForMass(threshold);
            const contours = extractContours(densityGrid, densityValue, bounds, gridSize);
            
            // Draw each contour
            contours.forEach(contour => {
                if (contour.length < 3) return;  // Need at least 3 points for a polygon
                
                // Create closed contour using Polygon for proper closed rendering
                const path = contour.map(p => new google.maps.LatLng(p.lat, p.lon));
                const color = getContourColor(threshold);
                
                // Ensure the path is properly closed
                const firstPoint = path[0];
                const lastPoint = path[path.length - 1];
                if (firstPoint.lat() !== lastPoint.lat() || firstPoint.lng() !== lastPoint.lng()) {
                    path.push(firstPoint);
                }
                
                // Use Polygon instead of Polyline for proper closed contours
                const polygon = new google.maps.Polygon({
                    paths: path,
                    strokeColor: color,
                    strokeOpacity: 0.9,
                    strokeWeight: 2.5,
                    fillColor: color,
                    // Shade to create bands: outer 90% green, then 70% yellow, 50% orange, 30% red (inner)
                    fillOpacity: 0.18,
                    map: map,
                    clickable: false,
                    zIndex: 10 + (3 - indexFromOuter)
                });
                
                // Place labels along the contour at different positions to avoid overlap
                // For concentric contours, stagger the label positions
                // Use different angles for each threshold: 0°, 45°, 90°, 135°, 180°
                const labelAngles = [90, 45, 0, 315, 270]; // Top, top-right, right, bottom-right, bottom
                const angleIndex = indexFromOuter % labelAngles.length;
                const targetAngle = labelAngles[angleIndex];
                
                // Find the point on the contour closest to the target angle from centroid
                let centroidLat = 0, centroidLon = 0;
                for (const point of path) {
                    centroidLat += point.lat();
                    centroidLon += point.lng();
                }
                centroidLat /= path.length;
                centroidLon /= path.length;
                
                // Find the point on the path closest to the target angle
                let bestPoint = path[0];
                let minAngleDiff = Infinity;
                
                for (const point of path) {
                    const angleDeg = Math.atan2(
                        point.lat() - centroidLat,
                        point.lng() - centroidLon
                    ) * 180 / Math.PI;
                    
                    // Normalize angle to 0-360
                    let normalizedAngle = (angleDeg + 360) % 360;
                    
                    // Calculate difference to target angle
                    let diff = Math.abs(normalizedAngle - targetAngle);
                    if (diff > 180) diff = 360 - diff;
                    
                    if (diff < minAngleDiff) {
                        minAngleDiff = diff;
                        bestPoint = point;
                    }
                }
                
                const labelPosition = bestPoint;
                
                // Create label marker with white background positioned on the contour ring
                const label = new google.maps.Marker({
                    position: labelPosition,
                    map: map,
                    icon: {
                        path: google.maps.SymbolPath.CIRCLE,
                        scale: 14,                 // larger circle to avoid clipping
                        fillColor: 'white',
                        fillOpacity: 0.95,
                        strokeColor: color,
                        strokeOpacity: 1,
                        strokeWeight: 2
                    },
                    label: {
                        text: `${Math.round(threshold * 100)}%`,
                        color: color,
                        fontSize: '10px',          // slightly smaller text
                        fontWeight: 'bold'
                    },
                    clickable: false,
                    zIndex: 20 + (3 - indexFromOuter)
                });
                
                contourLayers.push({ 
                    polyline: polygon,  // Actually a polygon, but kept name for compatibility
                    polyline2: null,    // No second segment needed
                    label: label, 
                    threshold 
                });
            });
        });
        
        // Add zoom listener to hide/show contours based on zoom level
        // Store listener reference so we can remove it later
        if (map._contourZoomListener) {
            google.maps.event.removeListener(map._contourZoomListener);
        }
        map._contourZoomListener = google.maps.event.addListener(map, 'zoom_changed', updateContourVisibility);
        updateContourVisibility();
        
        console.log(`Created ${contourLayers.length} contour layers`);
    } catch (error) {
        console.error('Error generating contours:', error);
    }
}

function createDensityGrid(points, bounds, gridSize) {
    // Create 2D grid for density calculation
    const grid = Array(gridSize).fill(0).map(() => Array(gridSize).fill(0));
    
    const latStep = (bounds.maxLat - bounds.minLat) / gridSize;
    const lonStep = (bounds.maxLon - bounds.minLon) / gridSize;
    
    // Kernel bandwidth (adjust based on data spread)
    const latRange = bounds.maxLat - bounds.minLat;
    const lonRange = bounds.maxLon - bounds.minLon;
    const bandwidth = Math.max(latRange, lonRange) * 0.1;  // 10% of range
    
    // Calculate density at each grid point using Gaussian kernel
    for (let i = 0; i < gridSize; i++) {
        for (let j = 0; j < gridSize; j++) {
            const gridLat = bounds.minLat + (i + 0.5) * latStep;
            const gridLon = bounds.minLon + (j + 0.5) * lonStep;
            
            let density = 0;
            for (const point of points) {
                const latDist = (point.lat - gridLat) / bandwidth;
                const lonDist = (point.lon - gridLon) / bandwidth;
                const distSq = latDist * latDist + lonDist * lonDist;
                // Gaussian kernel
                const kernelWeight = Math.exp(-distSq / 2);
                // Apply point weight (ensemble points weighted more heavily than Monte Carlo)
                const pointWeight = point.weight || 1.0;
                density += kernelWeight * pointWeight;
            }
            grid[i][j] = density;
        }
    }
    
    return grid;
}

function extractContours(densityGrid, threshold, bounds, gridSize) {
    // Extract contour polygons from density grid
    const contours = [];
    const latStep = (bounds.maxLat - bounds.minLat) / gridSize;
    const lonStep = (bounds.maxLon - bounds.minLon) / gridSize;
    
    // Find all grid cells above threshold
    const aboveThreshold = [];
    for (let i = 0; i < gridSize; i++) {
        for (let j = 0; j < gridSize; j++) {
            if (densityGrid[i][j] >= threshold) {
                const lat = bounds.minLat + (i + 0.5) * latStep;
                const lon = bounds.minLon + (j + 0.5) * lonStep;
                aboveThreshold.push({ lat, lon, i, j });
            }
        }
    }
    
    if (aboveThreshold.length === 0) return [];
    
    // Create boundary polygon from points above threshold
    // Use convex hull approach for simplicity
    const contour = createConvexHull(aboveThreshold.map(p => ({ lat: p.lat, lon: p.lon })));
    return contour.length > 0 ? [contour] : [];
}

function createConvexHull(points) {
    // Graham scan algorithm for convex hull
    if (points.length < 3) return [];
    
    // Find bottom-most point (or leftmost in case of tie)
    let bottom = 0;
    for (let i = 1; i < points.length; i++) {
        if (points[i].lat < points[bottom].lat || 
            (points[i].lat === points[bottom].lat && points[i].lon < points[bottom].lon)) {
            bottom = i;
        }
    }
    
    // Sort points by polar angle
    const sorted = [...points];
    const bottomPoint = sorted[bottom];
    sorted.splice(bottom, 1);
    
    sorted.sort((a, b) => {
        const angleA = Math.atan2(a.lat - bottomPoint.lat, a.lon - bottomPoint.lon);
        const angleB = Math.atan2(b.lat - bottomPoint.lat, b.lon - bottomPoint.lon);
        return angleA - angleB;
    });
    
    // Build convex hull
    const hull = [bottomPoint, sorted[0]];
    for (let i = 1; i < sorted.length; i++) {
        while (hull.length > 1 && 
               crossProduct(hull[hull.length - 2], hull[hull.length - 1], sorted[i]) <= 0) {
            hull.pop();
        }
        hull.push(sorted[i]);
    }
    
    // Close the polygon
    hull.push(bottomPoint);
    
    return hull;
}

function crossProduct(o, a, b) {
    return (a.lon - o.lon) * (b.lat - o.lat) - (a.lat - o.lat) * (b.lon - o.lon);
}

/**
 * Get color for contour line based on probability threshold.
 * 
 * Returns specific colors for standard thresholds (30%, 50%, 70%, 90%)
 * and fallback gradient colors for non-standard thresholds.
 * 
 * @param {number} threshold - Probability threshold (0.0 to 1.0)
 * @returns {string} Hex color code
 */
function getContourColor(threshold) {
    // Exact mapping requested: 30% red, 50% orange, 70% yellow, 90% green
    const t = Math.round(threshold * 100);
    if (t === 30) return '#C40000';  // slightly deeper red
    if (t === 50) return '#C25A00';  // stronger orange
    if (t === 70) return '#C0A000';  // richer yellow
    if (t === 90) return '#1F6A00';  // darker green for better contrast
    // Fallback gradient if non-standard threshold
    if (threshold >= 0.7) return '#C0A000';
    if (threshold >= 0.5) return '#C25A00';
    if (threshold >= 0.3) return '#C40000';
    return '#1F6A00';
}

/**
 * Update contour line visibility based on map zoom level.
 * 
 * Hides contours when zoomed out too far (zoom < 8) to reduce visual clutter.
 * Shows them when zoomed in (zoom >= 8) for detailed analysis.
 * 
 * Dependencies: Requires global map object and contourLayers array
 */
function updateContourVisibility() {
    const zoom = map.getZoom();
    // Hide contours when zoomed out too far (zoom < 8)
    // Show them when zoomed in (zoom >= 8)
    const shouldShow = zoom >= 8;
    
    contourLayers.forEach(layer => {
        if (layer.polyline) {
            layer.polyline.setMap(shouldShow ? map : null);
        }
        if (layer.polyline2) {
            layer.polyline2.setMap(shouldShow ? map : null);
        }
        if (layer.label) {
            layer.label.setMap(shouldShow ? map : null);
        }
    });
}

// ============================================================================
// SIMULATION ORCHESTRATION
// ============================================================================

/**
 * Main simulation function - orchestrates trajectory simulation requests.
 * 
 * Handles STANDARD balloon type and both single
 * and ensemble/multi modes. Includes debouncing, race condition protection,
 * progress tracking via SSE, and comprehensive error handling.
 * 
 * Flow:
 * 1. Debounce and race condition checks
 * 2. Parse input parameters from DOM
 * 3. Validate parameters based on balloon type
 * 4. Build API URL
 * 5. Execute simulation (single or ensemble)
 * 6. Handle progress updates via SSE (ensemble only)
 * 7. Render results (paths, heatmap, contours)
 * 
 * Dependencies:
 * - Requires DOM elements for input parameters
 * - Requires global map object for rendering
 * - Requires URL_ROOT constant for API endpoints
 * - Requires btype global variable for balloon type
 * 
 * Side effects:
 * - Clears previous visualizations
 * - Updates button/spinner UI state
 * - Makes fetch requests to backend API
 * - Renders paths, markers, heatmap, contours
 * - Updates progress tracking
 */
async function simulate() {
    // Prevent duplicate/overlapping simulation calls (race condition protection)
    // Check and set atomically to prevent multiple simultaneous calls
    // This is critical on mobile where double-taps can occur
    
    // Debounce: If called within 500ms of last call, ignore (prevents double-taps)
    const now = Date.now();
    if (window.__lastSimulateCall && (now - window.__lastSimulateCall) < 500) {
        console.log('Simulate called too soon after last call - ignoring (debounce protection)');
        return;
    }
    window.__lastSimulateCall = now;
    
    if (window.__simRunning) {
        // If a simulation is already running, interpret this call as a cancel request
        if (window.__simAbort) {
            try { window.__simAbort.abort(); } catch (e) {}
        }
        // Note: Ensemble mode on server will still expire after duration from when it was set
        console.log('Simulate called but already running - ignoring duplicate call');
        return;
    }
    
    // Set running flag IMMEDIATELY to prevent race conditions
    // This must happen before any async operations
    // Use a timestamp to help debug if duplicate calls slip through
    window.__simRunning = true;
    window.__simRunningStartTime = Date.now();
    window.__simAbort = new AbortController();
    console.log('Simulate started at', new Date().toISOString());
    
    // Clear previous simulation results immediately (paths, heatmap, and contours)
    clearAllVisualizations();

    const simBtn = document.getElementById('simulate-btn');
    const spinner = document.getElementById('sim-spinner');
    const originalButtonText = simBtn ? simBtn.textContent : null;
    
    // Store originalButtonText globally so it's available in finally block
    window.__originalButtonText = originalButtonText;
    
    if (simBtn) {
        simBtn.disabled = false; // Allow clicking to cancel
        simBtn.classList.add('loading');
        // Keep button clickable - clicking will call simulate() which detects running state and aborts
        // Don't set pointer-events: none - allow clicks to cancel
    }
    if (spinner) { spinner.classList.add('active'); }
    
    try {
        allValues = [];
        var time = toTimestamp(Number(document.getElementById('yr').value),
            Number(document.getElementById('mo').value),
            Number(document.getElementById('day').value),
            Number(document.getElementById('hr').value),
            Number(document.getElementById('mn').value));
        var lat = document.getElementById('lat').value;
        var lon = document.getElementById('lon').value;
        var alt = document.getElementById('alt').value;
        // Capture launch info for end pin details
        lastLaunchInfo = { time: time, lat: lat, lon: lon };
        
        // Update location marker and center map if coordinates are provided
        if (lat && lon && !isNaN(parseFloat(lat)) && !isNaN(parseFloat(lon))) {
            try {
                var position = new google.maps.LatLng(parseFloat(lat), parseFloat(lon));
                if (typeof updateClickMarker === 'function') {
                    updateClickMarker(position);
                }
                if (typeof map !== 'undefined' && map && typeof map.panTo === 'function') {
                    map.panTo(position);
                }
            } catch (e) {
                console.warn('Could not update location marker/map center:', e);
            }
        }
        
        var url = "";
        allValues.push(time,alt);
        // STANDARD mode: Get ascent, descent, and burst altitude
        var equil = document.getElementById('equil').value.trim();
        var eqtime = 0; // STANDARD mode uses 0 for eqtime
        var asc = document.getElementById('asc').value.trim();
        var desc = document.getElementById('desc').value.trim();
        
        // Validate all required parameters using helper functions
        validatePositiveNumber(asc, "Ascent rate");
        validatePositiveNumber(equil, "Burst altitude");
        validatePositiveNumber(desc, "Descent rate");
        
        url = URL_ROOT + "/singlezpb?timestamp="
            + time + "&lat=" + lat + "&lon=" + lon + "&alt=" + alt + "&equil=" + equil + "&eqtime=" + eqtime + "&asc=" + asc + "&desc=" + desc;
        allValues.push(equil,asc,desc);
        
        var onlyonce = true;
        // Validate ascent rate for STANDARD mode
        const validationPassed = checkNumPos(allValues) && checkasc(asc, alt, equil);
        
        // Only proceed with simulation if validation passed
        if (validationPassed) {
            // Validation passed - proceed with simulation
            // If "Multi" label is active AND the button is enabled, run multi
            const multiRequested = (window.ensembleEnabled === true) && (window.ensembleMultiLabel === true);
            if (multiRequested) {
                // Multi is supported for STANDARD mode
                window.multiActive = true;
                    // Staggered runs: every 3 hours from 0 to 168 (one week)
                    const offsets = [];
                    for (let h = 0; h <= 168; h += 3) offsets.push(h);
                    // Rainbow gradient colors (red -> purple)
                    const total = offsets.length;
                    function hslToHex(h, s, l) {
                        s /= 100; l /= 100;
                        const k = n => (n + h / 30) % 12;
                        const a = s * Math.min(l, 1 - l);
                        const f = n => l - a * Math.max(-1, Math.min(k(n) - 3, Math.min(9 - k(n), 1)));
                        const toHex = x => Math.round(255 * x).toString(16).padStart(2, '0');
                        return '#' + toHex(f(0)) + toHex(f(8)) + toHex(f(4));
                    }
                    function getMultiGradientColor(idx, n) {
                        if (n <= 1) return '#DC143C';
                        const hue = 0 + (270 * idx) / (n - 1);
                        return hslToHex(hue, 85, 50);
                    }
                    for (let i = 0; i < offsets.length; i++) {
                        const h = offsets[i];
                        const t2 = time + h * 3600;
                        const url2 = URL_ROOT + "/singlezpb?timestamp="
                            + t2 + "&lat=" + lat + "&lon=" + lon + "&alt=" + alt + "&equil=" + equil + "&eqtime=" + (eqtime || 0) + "&asc=" + asc + "&desc=" + desc
                            + "&model=0";
                        try {
                            const response = await fetch(url2, { signal: window.__simAbort.signal });
                            const payload = await response.json();
                            if (payload && payload !== "error" && payload !== "alt error") {
                                // Add end pin only; defer trajectory to endpoint click
                                const color = getMultiGradientColor(i, total);
                                addMultiEndPin(payload, h, color);
                            }
                        } catch (e) {
                            if (e && (e.name === 'AbortError' || e.message === 'The operation was aborted.')) {
                                break;
                            }
                            console.warn('Multi fetch failed for offset', h, e);
                        }
                    }
                    // Draw connector line between endpoints after multi completes
                    try {
                        if (multiEndPositions && multiEndPositions.length > 1) {
                            if (multiConnectorPath) { multiConnectorPath.setMap(null); multiConnectorPath = null; }
                            multiConnectorPath = new google.maps.Polyline({
                                path: multiEndPositions,
                                geodesic: true,
                                strokeColor: '#808080',
                                strokeOpacity: 0.6,
                                strokeWeight: 6,
                                zIndex: 1200
                            });
                            multiConnectorPath.setMap(map);
                        }
                    } catch (e) { console.warn('Failed to draw multi connector path', e); }
                    window.multiActive = false;
                    // Skip the rest of ensemble/single flow after multi
                    if (waypointsToggle) { showWaypoints(); }
                    return;
                }
            const isHistorical = Number(document.getElementById('yr').value) < 2019;
            // Determine which models to run based on server configuration
            const ensembleEnabled = window.ensembleEnabled || false;
            let modelIds;
            if (isHistorical) {
                // Historical data uses model 1 only
                modelIds = [1];
            } else if (ensembleEnabled) {
                // Ensemble mode: use all available models from server config
                // Fallback to [0, 1, 2] if config not available
                modelIds = window.availableModels || [0, 1, 2];
            } else {
                // Single model mode: use control model if available, else model 0
                modelIds = window.availableModels && window.availableModels.includes(0) ? [0] : [0];
            }

            // Use /spaceshot endpoint for parallel execution when ensemble is enabled (STANDARD mode)
            const useSpaceshot = ensembleEnabled && !isHistorical;
            
            if (useSpaceshot && modelIds.length > 1) {
                // Build URL and extract exact parameter values to match server's request_id generation
                const spaceshotUrl = URL_ROOT + "/spaceshot?timestamp="
                    + time + "&lat=" + lat + "&lon=" + lon + "&alt=" + alt 
                    + "&equil=" + equil + "&eqtime=" + eqtime 
                    + "&asc=" + asc + "&desc=" + desc;
                
                // Extract exact URL parameter values (as strings, matching server's request.args)
                // Parse URL to get exact parameter values that server will receive
                const urlObj = new URL(spaceshotUrl);
                const urlParams = urlObj.searchParams;
                const timestamp = urlParams.get('timestamp') || '';
                const urlLat = urlParams.get('lat') || '';
                const urlLon = urlParams.get('lon') || '';
                const urlAlt = urlParams.get('alt') || '';
                const urlEquil = urlParams.get('equil') || '';
                const urlEqtime = urlParams.get('eqtime') || '';
                const urlAsc = urlParams.get('asc') || '';
                const urlDesc = urlParams.get('desc') || '';
                
                // Server uses base_coeff as float (default 1.0), which becomes "1.0" in f-string
                // Must match exactly: float 1.0 -> string "1.0" (not "1")
                const baseCoeff = 1.0;
                const baseCoeffStr = baseCoeff.toFixed(1); // Ensures "1.0" format
                
                // Generate request_id using exact URL parameter values (matching server)
                const requestKey = `${timestamp}_${urlLat}_${urlLon}_${urlAlt}_${urlEquil}_${urlEqtime}_${urlAsc}_${urlDesc}_${baseCoeffStr}`;
                const clientRequestId = CryptoJS?.MD5 ? CryptoJS.MD5(requestKey).toString().substring(0, 16) : null;
                
                console.log(`[Client] Generated request_id: ${clientRequestId} from key: ${requestKey}`);
                
                // Set initial button text to "Starting..." instead of "0%" to avoid flash
                if (simBtn) simBtn.textContent = 'Starting...';
                
                let progressEventSource = null;
                let requestIdFromServer = null;
                
                const startProgressSSE = (requestId) => {
                    if (progressEventSource) {
                        progressEventSource.close();
                        progressEventSource = null;
                    }
                    
                    if (!simBtn) return;
                    
                    const sseUrl = URL_ROOT + "/progress-stream?request_id=" + requestId;
                    try {
                        progressEventSource = new EventSource(sseUrl);
                        // Keep "Starting..." text until first SSE message arrives
                        
                        progressEventSource.onmessage = function(event) {
                            try {
                                const data = JSON.parse(event.data);
                                if (data.error) {
                                    console.warn(`[${requestId}] SSE error:`, data.error);
                                    return;
                                }
                                if (simBtn) {
                                    // Show "Loading..." when loading files or when percentage is 0
                                    // Show percentage when simulations are actually running
                                    if (data.status === 'loading' || (data.percentage !== undefined && data.percentage === 0)) {
                                        simBtn.textContent = 'Loading...';
                                    } else if (data.percentage !== undefined && data.percentage > 0) {
                                        simBtn.textContent = data.percentage + '%';
                                    }
                                    if (data.percentage >= 100 && progressEventSource) {
                                        progressEventSource.close();
                                        progressEventSource = null;
                                    }
                                }
                            } catch (e) {
                                console.warn(`[${requestId}] Failed to parse SSE data:`, e);
                            }
                        };
                        
                        progressEventSource.onerror = function(event) {
                            // Connection errors are expected (closed, etc.) - not fatal
                        };
                    } catch (e) {
                        console.error(`[${requestId}] Failed to create SSE:`, e);
                        progressEventSource = null;
                    }
                };
                
                window.__inProgressMode = true;
                
                try {
                    const spaceshotPromise = fetch(spaceshotUrl, { signal: window.__simAbort.signal });
                    // Wait for server to receive request, parse args, generate request_id, and create progress tracking
                    // Increased delay to ensure progress tracking is initialized before SSE connects
                    await new Promise(resolve => setTimeout(resolve, 500)); // 500ms should be enough for server to initialize
                    
                    if (clientRequestId) {
                        startProgressSSE(clientRequestId);
                    }
                    
                    const response = await spaceshotPromise;
                    
                    // Check if response is OK before parsing
                    if (!response.ok) {
                        const errorText = await response.text().catch(() => 'Unknown error');
                        console.error('Spaceshot response not OK:', response.status, response.statusText, errorText);
                        throw new Error(`Server returned ${response.status}: ${response.statusText}`);
                    }
                    
                    // Parse JSON response
                    let data;
                    try {
                        const responseText = await response.text();
                        if (!responseText || responseText.trim().length === 0) {
                            throw new Error('Empty response from server');
                        }
                        data = JSON.parse(responseText);
                    } catch (parseError) {
                        console.error('Failed to parse spaceshot response as JSON:', parseError);
                        throw new Error('Server returned invalid JSON. The simulation may have timed out or failed.');
                    }
                    
                    console.log('Spaceshot response received:', {
                        isArray: Array.isArray(data),
                        hasPaths: !Array.isArray(data) && 'paths' in data,
                        hasHeatmapData: !Array.isArray(data) && 'heatmap_data' in data,
                        pathsLength: Array.isArray(data) ? data.length : (data.paths ? data.paths.length : 0),
                        heatmapLength: Array.isArray(data) ? 0 : (data.heatmap_data ? data.heatmap_data.length : 0),
                        sampleHeatmapPoint: !Array.isArray(data) && data.heatmap_data && data.heatmap_data.length > 0 ? data.heatmap_data[0] : null
                    });
                    
                    // Handle new response format (backward compatible)
                    let payloads, heatmapData;
                    if (Array.isArray(data)) {
                        // Legacy format: just array of paths
                        payloads = data;
                        heatmapData = [];
                        console.log('Using legacy array format (no heatmap data)');
                    } else {
                        // New format: object with paths and heatmap_data
                        payloads = data.paths || [];
                        heatmapData = data.heatmap_data || [];
                        requestIdFromServer = data.request_id || null;
                        console.log(`New format: ${payloads.length} paths, ${heatmapData.length} heatmap points`);
                    }
                    
                    // Switch to server's request_id if different (server is authoritative)
                    if (requestIdFromServer && requestIdFromServer !== clientRequestId) {
                        if (progressEventSource) {
                            progressEventSource.close();
                            progressEventSource = null;
                        }
                        startProgressSSE(requestIdFromServer);
                    }
                    
                    // Validate response structure
                    if (!payloads || !Array.isArray(payloads)) {
                        console.error('Invalid spaceshot response: payloads is not an array', data);
                        throw new Error('Server returned invalid response format. Expected array of paths.');
                    }
                    
                    if (payloads.length === 0 && !requestIdFromServer) {
                        console.error('Spaceshot returned empty payloads array');
                        throw new Error('Server returned no simulation results. The simulation may have failed.');
                    }
                    
                    // Clean up progress tracking when results are ready
                    if (payloads.length > 0) {
                        // Mark progress mode as complete
                        window.__inProgressMode = false;
                        if (progressEventSource) {
                            progressEventSource.close();
                            progressEventSource = null;
                        }
                    }

                    // Process ensemble paths (existing functionality)
                    // Note: payloads array order matches modelIds order from server config
                    if (payloads.length !== modelIds.length) {
                        console.warn(`Spaceshot returned ${payloads.length} results but expected ${modelIds.length} models`);
                    }
                    
                    // Count failed models for better error messaging
                    let failedCount = 0;
                    let altErrorCount = 0;
                    for (let i = 0; i < payloads.length && i < modelIds.length; i++) {
                        const payload = payloads[i];
                        if (payload === "error") {
                            failedCount++;
                        } else if (payload === "alt error") {
                            altErrorCount++;
                        }
                    }
                    
                    for (let i = 0; i < payloads.length && i < modelIds.length; i++) {
                        const payload = payloads[i];
                        const modelId = modelIds[i];
                        
                        if (payload === "error") {
                            console.error(`Model ${modelId} returned error`);
                            if (onlyonce) {
                                if (failedCount === payloads.length) {
                                    // All models failed
                                    alert("Simulation failed on the server. Please verify inputs or try again in a few minutes.");
                                } else {
                                    // Some models failed
                                    alert(`${failedCount} of ${modelIds.length} models failed to simulate. Some results may be incomplete.`);
                                }
                                onlyonce = false;
                            }
                        }
                        else if (payload === "alt error") {
                            console.error(`Model ${modelId} returned altitude error`);
                            if (onlyonce) {
                                alert("ERROR: Please make sure your entire flight altitude is within 45km.");
                                onlyonce = false;
                            }
                        }
                        else if (payload !== null && payload !== undefined) {
                            showpath(payload, modelId);
                        } else {
                            console.warn(`Model ${modelId} returned null/undefined result`);
                        }
                    }
                    
                    // Display Monte Carlo heatmap if data is available
                    console.log(`Heatmap data received: ${heatmapData ? heatmapData.length : 0} points`);
                    if (heatmapData && heatmapData.length > 0) {
                        console.log('Calling displayHeatmap with', heatmapData.length, 'points');
                        displayHeatmap(heatmapData);
                    } else {
                        console.warn('No heatmap data to display (heatmapData is empty or null)');
                    }
                } catch (error) {
                    // Clean up progress tracking
                    window.__inProgressMode = false;
                    if (progressEventSource) {
                        progressEventSource.close();
                        progressEventSource = null;
                    }
                    
                    if (error && (error.name === 'AbortError' || error.message === 'The operation was aborted.')) {
                        // Cancelled: stop processing
                        console.log('Spaceshot request was cancelled');
                    } else {
                        console.error('Spaceshot fetch failed', error);
                        if (onlyonce) {
                            // Provide more specific error messages
                            let errorMessage = 'Failed to contact simulation server. Please try again later.';
                            if (error.message && error.message.includes('timeout')) {
                                errorMessage = 'Simulation timed out. The request took too long. Please try again or use a shorter simulation time.';
                            } else if (error.message && error.message.includes('JSON')) {
                                errorMessage = 'Server returned invalid response. The simulation may have timed out or failed. Please try again.';
                            } else if (error.message && error.message.includes('Server returned')) {
                                errorMessage = error.message + '. Please try again later.';
                            }
                            alert(errorMessage);
                            onlyonce = false;
                        }
                    }
                }
            } else {
                // Sequential mode: loop through models one by one (for single model)
                for (const modelId of modelIds) {
                    const urlWithModel = url + "&model=" + modelId;
                    console.log(urlWithModel);
                    try {
                        const response = await fetch(urlWithModel, { signal: window.__simAbort.signal });
                        const payload = await response.json();

                        if (payload === "error") {
                            if (onlyonce) {
                                alert("Simulation failed on the server. Please verify inputs or try again in a few minutes.");
                                onlyonce = false;
                            }
                        }
                        else if (payload === "alt error") {
                            if (onlyonce) {
                                alert("ERROR: Please make sure your entire flight altitude is within 45km.");
                                onlyonce = false;
                            }
                        }
                        else {
                            showpath(payload, modelId);
                        }
                    } catch (error) {
                        if (error && (error.name === 'AbortError' || error.message === 'The operation was aborted.')) {
                            // Cancelled: stop processing further models, keep what is already drawn
                            break;
                        }
                        console.error('Simulation fetch failed', error);
                        if (onlyonce) {
                            alert('Failed to contact simulation server. Please try again later.');
                            onlyonce = false;
                        }
                    }
                }
            }
            
            // Show waypoints if toggle is enabled (only if validation passed)
            if (waypointsToggle) { 
                showWaypoints(); 
            }
        }
    } catch (error) {
        // Handle any unexpected errors during simulation
        console.error('Simulation error:', error);
        if (onlyonce) {
            alert('An unexpected error occurred during simulation. Please try again.');
            onlyonce = false;
        }
    } finally {
        window.__simRunning = false;
        window.__simAbort = null;
        // Keep elevation field value - only clear if parameters change
        // (Removed automatic clearing - elevation persists until user changes location)
        if (spinner) { spinner.classList.remove('active'); }
        // Re-enable button after simulation completes
        const simBtnFinal = document.getElementById('simulate-btn');
        if (simBtnFinal) {
            simBtnFinal.disabled = false;
            simBtnFinal.classList.remove('loading');
            // onclick handler from HTML remains (calls simulate())
            // Only restore original text if not in progress mode
            // This prevents overwriting progress percentage during ensemble simulations
            if (!window.__inProgressMode) {
                const currentText = simBtnFinal.textContent;
                if (window.__originalButtonText !== null && window.__originalButtonText !== undefined && 
                    !currentText.match(/^\d+%$/)) {
                    simBtnFinal.textContent = window.__originalButtonText;
                } else {
                    simBtnFinal.textContent = 'Simulate';
                }
            }
            // Reset progress mode flag
            window.__inProgressMode = false;
        }
        
        // Update print overlay with current simulation parameters
        // This ensures Cmd+P/Ctrl+P always has the latest data
        if (typeof window.updatePrintOverlay === 'function') {
            window.updatePrintOverlay();
        }
    }
}
