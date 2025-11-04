// Controls fetching and rendering of trajectories.

// Cache of compount paths
rawpathcache = []

// Shows a single compound path, mode unaware
function makepaths(btype, allpaths, isControl = false){
    rawpathcache.push(allpaths)
    for (index in allpaths) {
        var pathpoints = [];

        for (point in allpaths[index]){
                var position = {
                    lat: allpaths[index][point][1],
                    lng: allpaths[index][point][2],
                };
                pathpoints.push(position);
        }
        
        // Control model (gec00) gets thicker, solid line; perturbed models get thinner lines
        var drawpath = new google.maps.Polyline({
            path: pathpoints,
            geodesic: true,
            strokeColor: getcolor(index),
            strokeOpacity: isControl ? 1.0 : 0.7,
            strokeWeight: isControl ? 4.5 : 2
        });
        drawpath.setMap(map);
        currpaths.push(drawpath);
    }


}
function clearWaypoints() {
    //Loop through all the markers and remove
    for (var i = 0; i < circleslist.length; i++) {
        circleslist[i].setMap(null);
    }
    circleslist = [];
}

function showWaypoints() {
    for (i in rawpathcache) {
        allpaths = rawpathcache[i]
        for (index in allpaths) {
            for (point in allpaths[index]){
                (function () {
                    var position = {
                        lat: allpaths[index][point][1],
                        lng: allpaths[index][point][2],
                    };
                    if(waypointsToggle){
                        var circle = new google.maps.Circle({
                            strokeColor: getcolor(index),
                            strokeOpacity: 0.8,
                            strokeWeight: 2,
                            fillColor: getcolor(index),
                            fillOpacity: 0.35,
                            map: map,
                            center: position,
                            radius: 300,
                            clickable: true
                        });
                        circleslist.push(circle);
                        // multiplied by 1000 so that the argument is in milliseconds, not seconds.
                        var date = new Date(allpaths[index][point][0] * 1000);

                        // Hours part from the timestamp
                        var hours = date.getHours();
                        // Minutes part from the timestamp
                        var minutes = "0" + date.getMinutes();
                        // Seconds part from the timestamp
                        var seconds = "0" + date.getSeconds();

                        // Will display time in 10:30:23 format
                        var formattedTime = hours + ':' + minutes.substr(-2) + ':' + seconds.substr(-2);
                        var infowindow = new google.maps.InfoWindow({
                            content: '<div style="font-family: -apple-system, BlinkMacSystemFont, \'Segoe UI\', Roboto, \'Helvetica Neue\', Arial, sans-serif; padding: 4px 6px; line-height: 1.5;">' +
                                     '<div style="margin-bottom: 4px;"><strong style="font-weight: 600;">Altitude:</strong> ' + allpaths[index][point][3] + 'm</div>' +
                                     '<div><strong style="font-weight: 600;">Time:</strong> ' + formattedTime + '</div>' +
                                     '</div>'
                        });
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


// Cache of circles
circleslist = [];

// Shows a single compound path, but is mode-aware
function showpath(path, modelId = 1) {
    switch(btype) {
        case 'STANDARD':
            var rise = path[0];
            var equil = []
            var fall = path[2];
            var fpath = [];

            break;
        case 'ZPB':
            var rise = path[0];
            var equil = path[1]
            var fall = path[2];
            var fpath = [];
            break;

        case 'FLOAT':
            var rise = [];
            var equil = [];
            var fall = [];
            var fpath = path;
    }
    var allpaths = [rise, equil, fall, fpath];
    const isControl = (modelId === 0);
    makepaths(btype, allpaths, isControl);

}

function getcolor(index){
    switch(index){
        case '0':
            return '#DC143C';
        case '1':
            return '#0000FF';
        case '2':
            return '#000000';
        case '3': return '#000000';
    }
}

// Cache of polyline objects
var currpaths = new Array();

// ============================================================================
// HEATMAP VISUALIZATION: Monte Carlo Landing Probability Density
// ============================================================================
// Displays a probability density heatmap of landing positions from Monte Carlo
// simulations. The heatmap shows where the balloon is most likely to land based
// on 420 simulations (20 parameter perturbations × 21 weather models).
//
// How it works:
// 1. Receives array of landing positions from server (420 points: {lat, lon})
// 2. Converts positions to Google Maps LatLng objects with equal weight
// 3. Creates HeatmapLayer that aggregates nearby points into density contours
// 4. Color gradient: cyan (low density) → green → yellow → orange → red (high)
// 5. Red areas indicate high probability landing zones (many simulations landed there)
//
// Visualization properties:
// - dissipating: false - maintains intensity across zoom levels (no splotchy appearance)
// - radius: 20 pixels - size of influence area for each point
// - opacity: 0.6 - allows seeing map/ensemble paths underneath
// - gradient: Color scale from transparent cyan to solid red based on density
// ============================================================================
function displayHeatmap(heatmapData) {
    try {
        // Check if Google Maps API is loaded
        if (!window.google || !window.google.maps) {
            console.error('Google Maps API not loaded yet. Waiting...');
            setTimeout(() => displayHeatmap(heatmapData), 1000);
            return;
        }
        
        // Check if visualization library is loaded (required for HeatmapLayer)
        if (!google.maps.visualization || !google.maps.visualization.HeatmapLayer) {
            console.error('Google Maps visualization library not loaded. Attempting to load...');
            // Try loading visualization library dynamically
            const script = document.createElement('script');
            script.src = 'https://maps.googleapis.com/maps/api/js?key=AIzaSyC62-iKLT_54_N0cPnbQlrzIsEKQxiAJgA&libraries=visualization&callback=function(){}';
            script.onload = () => {
                console.log('Visualization library loaded, retrying heatmap display');
                displayHeatmap(heatmapData);
            };
            document.head.appendChild(script);
            return;
        }
        
        // Check if map is initialized
        if (!map) {
            console.error('Map not initialized yet');
            setTimeout(() => displayHeatmap(heatmapData), 500);
            return;
        }
        
        // Clear existing heatmap if any (prevents overlapping heatmaps)
        if (heatmapLayer) {
            heatmapLayer.setMap(null);
            heatmapLayer = null;
        }
        
        if (!heatmapData || heatmapData.length === 0) {
            console.log('No heatmap data to display');
            return;
        }
        
        console.log(`Creating heatmap with ${heatmapData.length} landing positions`);
        
        // Convert landing positions to Google Maps LatLng objects with weight
        // Each landing position gets equal weight (weight: 1) - the heatmap
        // library automatically aggregates nearby points to create density contours
        const heatmapPoints = heatmapData.map(point => {
            // Validate point has lat/lon
            if (typeof point.lat !== 'number' || typeof point.lon !== 'number') {
                console.warn('Invalid heatmap point:', point);
                return null;
            }
            // Normalize longitude to [-180, 180] for display (Google Maps expects this range)
            let lon = point.lon;
            if (lon > 180) {
                lon = ((lon + 180) % 360) - 180;
            }
            return {
                location: new google.maps.LatLng(point.lat, lon),
                weight: 1  // Each landing contributes equally to density calculation
            };
        }).filter(p => p !== null); // Remove invalid points
        
        if (heatmapPoints.length === 0) {
            console.warn('No valid heatmap points after filtering');
            return;
        }
        
        console.log(`Creating heatmap layer with ${heatmapPoints.length} valid points`);
        
        // Create Google Maps HeatmapLayer
        // This layer automatically aggregates nearby points into density contours
        // and applies the color gradient based on point density
        heatmapLayer = new google.maps.visualization.HeatmapLayer({
            data: heatmapPoints,  // Array of {location: LatLng, weight: number}
            map: map,
            radius: 20,  // Radius of influence for each point (in pixels)
            opacity: 0.6,  // Opacity of the heatmap (allows seeing map underneath)
            gradient: [
                'rgba(0, 255, 255, 0)',      // Cyan (transparent) - low density
                'rgba(0, 255, 255, 0.5)',    // Cyan - medium-low
                'rgba(0, 255, 0, 0.7)',      // Green - medium
                'rgba(255, 255, 0, 0.8)',    // Yellow - medium-high
                'rgba(255, 165, 0, 0.9)',    // Orange - high
                'rgba(255, 0, 0, 1)'         // Red - highest density (most landing positions)
            ],
            dissipating: false,  // Keep intensity constant across zoom levels (prevents splotchy appearance)
            maxIntensity: 10    // Maximum intensity for normalization (caps density calculation)
        });
        
        console.log(`Heatmap displayed successfully with ${heatmapPoints.length} points`);
    } catch (error) {
        console.error('Error displaying heatmap:', error);
        console.error('Heatmap data:', heatmapData);
    }
}

// Progress tracking for ensemble simulations
let ensembleProgressInterval = null;
let ensembleStartTime = null;
let currentRequestId = null;

function updateEnsembleProgress(progressData) {
    const ensembleBtn = document.getElementById('ensemble-toggle');
    if (!ensembleBtn || !window.ensembleEnabled) return;
    
    const completed = progressData.completed || 0;
    const total = progressData.total || 441; // 21 ensemble + 420 Monte Carlo
    
    // Update button text - replace "Ensemble" with just the count (e.g., "X/441")
    // This prevents text from being cut off on mobile
    // No visual progress bar needed since count is displayed directly
    ensembleBtn.textContent = `${completed}/${total}`;
}

async function pollProgress(requestId) {
    if (!requestId) return null;
    
    try {
        const response = await fetch(`${URL_ROOT}/progress?request_id=${requestId}`);
        if (response.ok) {
            const data = await response.json();
            return data;
        }
    } catch (error) {
        console.warn('Progress polling failed:', error);
    }
    return null;
}

function clearEnsembleProgress() {
    if (ensembleProgressInterval) {
        clearInterval(ensembleProgressInterval);
        ensembleProgressInterval = null;
    }
    ensembleStartTime = null;
    currentRequestId = null;
    
    const ensembleBtn = document.getElementById('ensemble-toggle');
    if (ensembleBtn) {
        // Restore "Ensemble" text when simulation finishes or is cancelled
        ensembleBtn.textContent = 'Ensemble';
        // Re-apply the 'on' class if ensemble is still enabled
        if (window.ensembleEnabled) {
            ensembleBtn.classList.add('on');
        }
    }
}

// Self explanatory
async function simulate() {
    // Clear previous simulation results immediately (paths and heatmap)
    clearWaypoints();
    for (path in currpaths) {currpaths[path].setMap(null);}
    currpaths = new Array();
    // Clear heatmap - ensure it's removed before starting new simulation
    if (heatmapLayer) {
        heatmapLayer.setMap(null);
        heatmapLayer = null;
    }
    rawpathcache = new Array();
    console.log("Clearing previous simulation");
    
    // If a simulation is already running, interpret this call as a cancel request
    if (window.__simRunning && window.__simAbort) {
        try { window.__simAbort.abort(); } catch (e) {}
        clearEnsembleProgress();
        // Note: Ensemble mode on server will still expire after 60 seconds from when it was set
        // This is expected behavior - server doesn't know about client-side cancellation
        return;
    }

    // Setup abort controller for this run
    window.__simAbort = new AbortController();
    window.__simRunning = true;

    const simBtn = document.getElementById('simulate-btn');
    const spinner = document.getElementById('sim-spinner');
    const originalButtonText = simBtn ? simBtn.textContent : null;
    
    // Start progress tracking for ensemble mode
    if (window.ensembleEnabled) {
        clearEnsembleProgress();
        ensembleStartTime = Date.now();
        // Progress will be updated when we get request_id from response
        // For now, show initial state
        updateEnsembleProgress({completed: 0, total: 441, percentage: 0});
    }
    
    if (simBtn) {
        simBtn.disabled = true;
        simBtn.classList.add('loading');
        simBtn.disabled = false; // allow click to cancel
        simBtn.textContent = 'Simulating…';
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
        var url = "";
        allValues.push(time,alt);
        var equil, eqtime, asc, desc; // Declare variables for use in spaceshot URL
        switch(btype) {
            case 'STANDARD':
                equil = document.getElementById('equil').value;
                eqtime = 0; // STANDARD mode uses 0 for eqtime
                asc = document.getElementById('asc').value;
                desc = document.getElementById('desc').value;
                url = URL_ROOT + "/singlezpb?timestamp="
                    + time + "&lat=" + lat + "&lon=" + lon + "&alt=" + alt + "&equil=" + equil + "&eqtime=" + eqtime + "&asc=" + asc + "&desc=" + desc;
                allValues.push(equil,asc,desc);
                break;
            case 'ZPB':
                equil = document.getElementById('equil').value;
                eqtime = document.getElementById('eqtime').value;
                asc = document.getElementById('asc').value;
                desc = document.getElementById('desc').value;
                url = URL_ROOT + "/singlezpb?timestamp="
                    + time + "&lat=" + lat + "&lon=" + lon + "&alt=" + alt + "&equil=" + equil + "&eqtime=" + eqtime + "&asc=" + asc + "&desc=" + desc
                allValues.push(equil,eqtime,asc,desc);
                break;
            case 'FLOAT':
                var coeff = document.getElementById('coeff').value;
                var step = document.getElementById('step').value;
                var dur = document.getElementById('dur').value;
                url = URL_ROOT + "/singlepredict?timestamp="
                    + time + "&lat=" + lat + "&lon=" + lon + "&alt=" + alt + "&rate=0&coeff=" + coeff + "&step=" + step + "&dur=" + dur
                allValues.push(coeff,step,dur);
                break;
        }
        var onlyonce = true;
        if(checkNumPos(allValues) && checkasc(asc,alt,equil)){
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

            // Use /spaceshot endpoint for parallel execution when ensemble is enabled and not FLOAT mode
            const useSpaceshot = ensembleEnabled && !isHistorical && (btype === 'STANDARD' || btype === 'ZPB');
            
            if (useSpaceshot && modelIds.length > 1) {
                // Use parallel spaceshot endpoint for ensemble runs (now includes Monte Carlo)
                const spaceshotUrl = URL_ROOT + "/spaceshot?timestamp="
                    + time + "&lat=" + lat + "&lon=" + lon + "&alt=" + alt 
                    + "&equil=" + equil + "&eqtime=" + eqtime 
                    + "&asc=" + asc + "&desc=" + desc;
                console.log("Using spaceshot endpoint (with Monte Carlo):", spaceshotUrl);
                
                // Start polling progress BEFORE fetch (so we can track progress during the long-running request)
                if (window.ensembleEnabled) {
                    // Compute request_id from parameters (same as server's hash function)
                    const requestKey = `${time}_${lat}_${lon}_${alt}_${equil}_${eqtime}_${asc}_${desc}`;
                    
                    // Simple hash function (matches server-side implementation)
                    let hash = 0;
                    for (let i = 0; i < requestKey.length; i++) {
                        hash = ((hash << 5) - hash) + requestKey.charCodeAt(i);
                        hash = hash & 0xFFFFFFFF; // Convert to 32-bit integer
                    }
                    currentRequestId = Math.abs(hash).toString(16).padStart(16, '0').substring(0, 16);
                    
                    // Start polling immediately (before fetch completes)
                    ensembleProgressInterval = setInterval(async () => {
                        if (currentRequestId && window.__simRunning) {
                            const progressData = await pollProgress(currentRequestId);
                            if (progressData) {
                                updateEnsembleProgress(progressData);
                                // If completed, stop polling
                                if (progressData.completed >= progressData.total) {
                                    if (ensembleProgressInterval) {
                                        clearInterval(ensembleProgressInterval);
                                        ensembleProgressInterval = null;
                                    }
                                }
                            }
                        } else {
                            // Simulation stopped, stop polling
                            if (ensembleProgressInterval) {
                                clearInterval(ensembleProgressInterval);
                                ensembleProgressInterval = null;
                            }
                        }
                    }, 2000); // Poll every 2 seconds
                }
                
                try {
                    const response = await fetch(spaceshotUrl, { signal: window.__simAbort.signal });
                    const data = await response.json(); // Now returns {paths: [...], heatmap_data: [...], request_id: ...}
                    
                    console.log('Spaceshot response received:', {
                        isArray: Array.isArray(data),
                        hasPaths: !Array.isArray(data) && 'paths' in data,
                        hasHeatmapData: !Array.isArray(data) && 'heatmap_data' in data,
                        pathsLength: Array.isArray(data) ? data.length : (data.paths ? data.paths.length : 0),
                        heatmapLength: Array.isArray(data) ? 0 : (data.heatmap_data ? data.heatmap_data.length : 0),
                        sampleHeatmapPoint: !Array.isArray(data) && data.heatmap_data && data.heatmap_data.length > 0 ? data.heatmap_data[0] : null
                    });
                    
                    // Handle new response format (backward compatible)
                    let payloads, heatmapData, requestId;
                    if (Array.isArray(data)) {
                        // Legacy format: just array of paths
                        payloads = data;
                        heatmapData = [];
                        requestId = null;
                        console.log('Using legacy array format (no heatmap data)');
                    } else {
                        // New format: object with paths and heatmap_data
                        payloads = data.paths || [];
                        heatmapData = data.heatmap_data || [];
                        requestId = data.request_id || null;
                        console.log(`New format: ${payloads.length} paths, ${heatmapData.length} heatmap points`);
                        // Update request_id if server provided one (should match our computed one)
                        if (requestId) {
                            currentRequestId = requestId;
                        }
                    }
                    
                    // Stop polling once fetch completes (simulation is done)
                    if (ensembleProgressInterval) {
                        clearInterval(ensembleProgressInterval);
                        ensembleProgressInterval = null;
                    }

                    // Process ensemble paths (existing functionality)
                    // Note: payloads array order matches modelIds order from server config
                    if (payloads.length !== modelIds.length) {
                        console.warn(`Spaceshot returned ${payloads.length} results but expected ${modelIds.length} models`);
                    }
                    for (let i = 0; i < payloads.length && i < modelIds.length; i++) {
                        const payload = payloads[i];
                        const modelId = modelIds[i];
                        
                        if (payload === "error") {
                            console.error(`Model ${modelId} returned error`);
                            if (onlyonce) {
                                alert("Simulation failed on the server. Please verify inputs or try again in a few minutes.");
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
                    if (error && (error.name === 'AbortError' || error.message === 'The operation was aborted.')) {
                        // Cancelled: stop processing
                    } else {
                        console.error('Spaceshot fetch failed', error);
                        if (onlyonce) {
                            alert('Failed to contact simulation server. Please try again later.');
                            onlyonce = false;
                        }
                    }
                }
            } else {
                // Sequential mode: loop through models one by one (for single model or FLOAT mode)
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
            onlyonce = true;
        }
        if (waypointsToggle) {showWaypoints()}
    } finally {
        window.__simRunning = false;
        clearEnsembleProgress(); // Clear progress when simulation completes
        window.__simAbort = null;
        // Blank out elevation to require refetch before next simulation
        try {
            var altInput = document.getElementById('alt');
            if (altInput) altInput.value = '';
        } catch (e) {}
        if (spinner) { spinner.classList.remove('active'); }
        if (simBtn) {
            simBtn.disabled = false;
            simBtn.classList.remove('loading');
            if (originalButtonText !== null) simBtn.textContent = originalButtonText;
        }
    }
}
