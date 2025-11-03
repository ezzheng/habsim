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

// Self explanatory
async function simulate() {
    // If a simulation is already running, interpret this call as a cancel request
    if (window.__simRunning && window.__simAbort) {
        try { window.__simAbort.abort(); } catch (e) {}
        return;
    }

    // Setup abort controller for this run
    window.__simAbort = new AbortController();
    window.__simRunning = true;

    const simBtn = document.getElementById('simulate-btn');
    const spinner = document.getElementById('sim-spinner');
    const originalButtonText = simBtn ? simBtn.textContent : null;
    if (simBtn) {
        simBtn.disabled = true;
        simBtn.classList.add('loading');
        simBtn.disabled = false; // allow click to cancel
        simBtn.textContent = 'Simulating…';
    }
    if (spinner) { spinner.classList.add('active'); }
    try {
        clearWaypoints();
        for (path in currpaths) {currpaths[path].setMap(null);}
        currpaths = new Array();
        rawpathcache = new Array()
        console.log("Clearing");

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
                // Use parallel spaceshot endpoint for ensemble runs
                const spaceshotUrl = URL_ROOT + "/spaceshot?timestamp="
                    + time + "&lat=" + lat + "&lon=" + lon + "&alt=" + alt 
                    + "&equil=" + equil + "&eqtime=" + eqtime 
                    + "&asc=" + asc + "&desc=" + desc;
                console.log("Using spaceshot endpoint:", spaceshotUrl);
                try {
                    const response = await fetch(spaceshotUrl, { signal: window.__simAbort.signal });
                    const payloads = await response.json(); // Array of results, one per model

                    // Process each result in the array
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
