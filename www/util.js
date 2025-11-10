//Maps initialization
var element = document.getElementById("map");
var map = new google.maps.Map(element, {
    center: new google.maps.LatLng(37.4, -121.5),
    zoom: 9,
    mapTypeId: "OSM",
    zoomControl: false,
    gestureHandling: 'greedy',
    mapTypeControl: false, // Disable default control - we'll use custom
    fullscreenControl: true,
    fullscreenControlOptions: {
        position: google.maps.ControlPosition.BOTTOM_RIGHT
    },
    streetViewControl: false
});
var clickMarker = null;
var heatmapLayer = null; // Global heatmap layer for Monte Carlo visualization
google.maps.event.addListener(map, 'click', function (event) {
    displayCoordinates(event.latLng);
});

// Custom map type control with drop-up menu
(function() {
    // Wait for map to be ready
    google.maps.event.addListenerOnce(map, 'idle', function() {
        // Create custom control container
        const controlDiv = document.createElement('div');
        controlDiv.id = 'custom-map-type-control';
        controlDiv.style.cssText = 'margin: 10px; position: absolute; bottom: 0; left: 0; z-index: 1000;';
        
        // Create control button (styled like Google Maps control)
        const controlButton = document.createElement('button');
        controlButton.type = 'button';
        controlButton.style.cssText = `
            background-color: white;
            border: none;
            border-radius: 2px;
            box-shadow: 0 1px 4px rgba(0,0,0,0.3);
            cursor: pointer;
            padding: 0;
            width: 40px;
            height: 40px;
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 18px;
            color: #5B5B5B;
            transition: background-color 0.1s;
            font-family: Roboto, Arial, sans-serif;
        `;
        controlButton.innerHTML = '🗺️';
        controlButton.title = 'Map type';
        
        // Hover effect for button
        controlButton.onmouseenter = function() {
            this.style.backgroundColor = '#f5f5f5';
        };
        controlButton.onmouseleave = function() {
            this.style.backgroundColor = 'white';
        };
        
        // Create dropdown menu
        const dropdownMenu = document.createElement('div');
        dropdownMenu.id = 'custom-map-type-menu';
        dropdownMenu.style.cssText = `
            position: absolute;
            bottom: 45px;
            left: 0;
            background-color: white;
            border-radius: 2px;
            box-shadow: 0 2px 6px rgba(0,0,0,0.3);
            min-width: 140px;
            display: none;
            flex-direction: column;
            overflow: hidden;
            z-index: 1001;
            font-family: Roboto, Arial, sans-serif;
        `;
        
        // Map type options
        const mapTypes = [
            { id: 'OSM', label: 'Map', icon: '🗺️' },
            { id: 'roadmap', label: 'Roadmap', icon: '🛣️' },
            { id: 'satellite', label: 'Satellite', icon: '🛰️' },
            { id: 'hybrid', label: 'Hybrid', icon: '🌍' },
            { id: 'terrain', label: 'Terrain', icon: '⛰️' }
        ];
        
        // Create menu items
        mapTypes.forEach(function(mapType) {
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
            
            // Hover effect
            menuItem.onmouseenter = function() {
                this.style.backgroundColor = '#f5f5f5';
            };
            menuItem.onmouseleave = function() {
                this.style.backgroundColor = 'white';
            };
            
            // Click handler
            menuItem.onclick = function() {
                try {
                    map.setMapTypeId(mapType.id);
                    updateActiveMapType(mapType.id);
                    dropdownMenu.style.display = 'none';
                } catch(e) {
                    console.warn('Map type not available:', mapType.id);
                }
            };
            
            dropdownMenu.appendChild(menuItem);
        });
        
        // Remove border from last item
        const lastItem = dropdownMenu.lastElementChild;
        if (lastItem) {
            lastItem.style.borderBottom = 'none';
        }
        
        // Toggle dropdown on button click
        controlButton.onclick = function(e) {
            e.stopPropagation();
            const isVisible = dropdownMenu.style.display === 'flex';
            dropdownMenu.style.display = isVisible ? 'none' : 'flex';
            
            // Update active state
            if (!isVisible) {
                updateActiveMapType(map.getMapTypeId());
            }
        };
        
        // Close dropdown when clicking outside
        document.addEventListener('click', function(e) {
            if (!controlDiv.contains(e.target)) {
                dropdownMenu.style.display = 'none';
            }
        });
        
        // Update active map type indicator
        function updateActiveMapType(activeId) {
            const items = dropdownMenu.querySelectorAll('button');
            items.forEach(function(item, index) {
                if (mapTypes[index].id === activeId) {
                    item.style.backgroundColor = '#e8f0fe';
                    item.style.color = '#1a73e8';
                } else {
                    item.style.backgroundColor = 'white';
                    item.style.color = '#5B5B5B';
                }
            });
        }
        
        // Assemble control
        controlDiv.appendChild(controlButton);
        controlDiv.appendChild(dropdownMenu);
        
        // Add to map
        map.controls[google.maps.ControlPosition.BOTTOM_LEFT].push(controlDiv);
        
        // Initialize active state
        updateActiveMapType(map.getMapTypeId());
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
                document.getElementById("alt").value = result;
            } else if (result && result.error) {
                throw new Error(result.error);
            } else {
                document.getElementById("alt").value = result;
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
