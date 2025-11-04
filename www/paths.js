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

// Contour layers for probability visualization
var contourLayers = [];  // Array of {polyline, label} objects
var contourLabels = [];  // Array of label markers

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

class CustomHeatmapOverlay extends google.maps.OverlayView {
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
        
        // Add canvas to panes
        const panes = this.getPanes();
        panes.overlayLayer.appendChild(this.canvas);
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
                    let weight = 0;
                    if (this.smoothingType === 'none') {
                        // No smoothing: raw point count (use small radius for binning)
                        if (distSq < (bandwidth * 0.1) ** 2) {
                            weight = 1;
                        }
                    } else if (this.smoothingType === 'epanechnikov') {
                        // Epanechnikov kernel (more rectangular, preserves shape better)
                        if (dist <= 1) {
                            weight = (1 - distSq) * 3 / 4; // Epanechnikov kernel
                        }
                    } else if (this.smoothingType === 'uniform') {
                        // Uniform kernel (rectangular)
                        if (dist <= 1) {
                            weight = 1;
                        }
                    } else if (this.smoothingType === 'gaussian') {
                        // Gaussian kernel (smooth but circular)
                        weight = Math.exp(-distSq / 2);
                    }
                    
                    density += weight;
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
}

// ============================================================================
// HEATMAP VISUALIZATION: Monte Carlo Landing Probability Density
// ============================================================================
// Displays a probability density heatmap of landing positions from Monte Carlo
// simulations using a custom canvas overlay that preserves the actual data shape.
//
// How it works:
// 1. Receives array of landing positions from server (420 points: {lat, lon})
// 2. Creates custom overlay with controllable smoothing (no forced circular patterns)
// 3. Renders density grid on canvas with custom color gradient
// 4. Color gradient: cyan (low density) → green → yellow → orange → red (high)
// 5. Red areas indicate high probability landing zones (many simulations landed there)
//
// Smoothing options:
// - 'none': Raw density grid (no smoothing, preserves exact shape)
// - 'epanechnikov': Epanechnikov kernel (rectangular-like, preserves shape well)
// - 'uniform': Uniform kernel (rectangular, preserves shape)
// - 'gaussian': Gaussian kernel (smooth but circular - default Google Maps behavior)
// ============================================================================
function displayHeatmap(heatmapData) {
    try {
        // Check if Google Maps API is loaded
        if (!window.google || !window.google.maps) {
            console.error('Google Maps API not loaded yet. Waiting...');
            setTimeout(() => displayHeatmap(heatmapData), 1000);
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
            if (heatmapLayer.setMap) {
                heatmapLayer.setMap(null);
            } else if (heatmapLayer.onRemove) {
                heatmapLayer.onRemove();
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
            return { lat: point.lat, lon: lon };
        }).filter(p => p !== null); // Remove invalid points
        
        if (heatmapPoints.length === 0) {
            console.warn('No valid heatmap points after filtering');
            return;
        }
        
        console.log(`Creating custom heatmap overlay with ${heatmapPoints.length} valid points`);
        
        // Create custom heatmap overlay
        // Options: 'none' (raw), 'epanechnikov' (recommended), 'uniform', 'gaussian'
        heatmapLayer = new CustomHeatmapOverlay(heatmapPoints, {
            opacity: 0.6,
            smoothingType: 'epanechnikov',  // Change to 'none' for raw density, 'gaussian' for smooth circular
            smoothingBandwidth: null,        // null = auto-calculate (5% of data range)
            gridResolution: 100,             // Higher = smoother but slower
            gradient: [
                {stop: 0.0, color: 'rgba(0, 255, 255, 0)'},      // Cyan (transparent) - low density
                {stop: 0.2, color: 'rgba(0, 255, 255, 0.5)'},    // Cyan - medium-low
                {stop: 0.4, color: 'rgba(0, 255, 0, 0.7)'},      // Green - medium
                {stop: 0.6, color: 'rgba(255, 255, 0, 0.8)'},    // Yellow - medium-high
                {stop: 0.8, color: 'rgba(255, 165, 0, 0.9)'},     // Orange - high
                {stop: 1.0, color: 'rgba(255, 0, 0, 1)'}         // Red - highest density
            ]
        });
        
        heatmapLayer.setMap(map);
        
        // Redraw on zoom/pan to update heatmap
        google.maps.event.addListener(map, 'bounds_changed', () => {
            if (heatmapLayer) {
                heatmapLayer.draw();
            }
        });
        
        console.log(`Custom heatmap displayed successfully with ${heatmapPoints.length} points`);
        
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
    contourLayers.forEach(layer => {
        if (layer.polyline) layer.polyline.setMap(null);
        if (layer.label) layer.label.setMap(null);
    });
    contourLayers = [];
    contourLabels = [];
}

function displayContours(heatmapData) {
    try {
        if (!heatmapData || heatmapData.length === 0) return;
        if (!map) return;
        
        // Normalize and validate points
        const points = heatmapData.map(point => {
            if (typeof point.lat !== 'number' || typeof point.lon !== 'number') return null;
            let lon = point.lon;
            if (lon > 180) lon = ((lon + 180) % 360) - 180;
            return { lat: point.lat, lon: lon };
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
        
        // Extract contours at probability thresholds
        const thresholds = [0.1, 0.3, 0.5, 0.7, 0.9];  // 10%, 30%, 50%, 70%, 90%
        const maxDensity = Math.max(...densityGrid.flat());
        
        thresholds.forEach((threshold, index) => {
            const densityValue = threshold * maxDensity;
            const contours = extractContours(densityGrid, densityValue, bounds, gridSize);
            
            // Draw each contour
            contours.forEach(contour => {
                if (contour.length < 3) return;  // Need at least 3 points for a polygon
                
                // Create contour polyline
                const path = contour.map(p => new google.maps.LatLng(p.lat, p.lon));
                const color = getContourColor(threshold);
                
                const polyline = new google.maps.Polyline({
                    path: path,
                    geodesic: true,
                    strokeColor: color,
                    strokeOpacity: 0.8,
                    strokeWeight: 2,
                    map: map,
                    zIndex: 1000 + index  // Higher z-index for higher probability
                });
                
                // Add label at midpoint of contour
                const midPoint = path[Math.floor(path.length / 2)];
                const label = new google.maps.Marker({
                    position: midPoint,
                    map: map,
                    icon: {
                        path: google.maps.SymbolPath.CIRCLE,
                        scale: 0,  // Invisible marker
                        fillColor: color,
                        fillOpacity: 0,
                        strokeColor: color,
                        strokeOpacity: 0
                    },
                    label: {
                        text: `${Math.round(threshold * 100)}%`,
                        color: color,
                        fontSize: '12px',
                        fontWeight: 'bold',
                        className: 'contour-label'
                    },
                    zIndex: 1001 + index
                });
                
                contourLayers.push({ polyline, label, threshold });
            });
        });
        
        // Add zoom listener to hide/show contours based on zoom level
        updateContourVisibility();
        google.maps.event.addListener(map, 'zoom_changed', updateContourVisibility);
        
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
                density += Math.exp(-distSq / 2);
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

function getContourColor(threshold) {
    // Color gradient based on probability threshold
    if (threshold >= 0.7) return '#FF0000';      // Red - high probability
    if (threshold >= 0.5) return '#FF8800';      // Orange
    if (threshold >= 0.3) return '#FFFF00';      // Yellow
    return '#00FF00';                             // Green - lower probability
}

function updateContourVisibility() {
    const zoom = map.getZoom();
    // Hide contours when zoomed out too far (zoom < 8)
    // Show them when zoomed in (zoom >= 8)
    const shouldShow = zoom >= 8;
    
    contourLayers.forEach(layer => {
        if (layer.polyline) {
            layer.polyline.setMap(shouldShow ? map : null);
        }
        if (layer.label) {
            layer.label.setMap(shouldShow ? map : null);
        }
    });
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
    const percentage = progressData.percentage || Math.round((completed / total) * 100);
    
    // Show percentage instead of count for better progress indication
    // Percentage updates more smoothly as simulations complete
    if (completed === 0) {
        ensembleBtn.textContent = '0%';
    } else if (completed >= total) {
        ensembleBtn.textContent = '100%';
    } else {
        ensembleBtn.textContent = `${percentage}%`;
    }
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
    // Clear previous simulation results immediately (paths, heatmap, and contours)
    clearWaypoints();
    for (path in currpaths) {currpaths[path].setMap(null);}
    currpaths = new Array();
    // Clear heatmap - ensure it's removed before starting new simulation
    if (heatmapLayer) {
        if (heatmapLayer.setMap) {
            heatmapLayer.setMap(null);
        } else if (heatmapLayer.onRemove) {
            heatmapLayer.onRemove();
        }
        heatmapLayer = null;
    }
    clearContours();  // Clear contour lines and labels
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
                    }, 500); // Poll every 500ms for smoother progress updates
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
