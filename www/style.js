/**
 * Balloon type and mode management module.
 * 
 * Handles:
 * - Balloon type selection (STANDARD, ZPB, FLOAT)
 * - Mode switching and UI visibility
 * - Default value initialization
 * - Server status polling
 * - Waypoint toggle functionality
 */

// ============================================================================
// GLOBAL STATE
// ============================================================================

/** Current balloon type: "STANDARD", "ZPB", or "FLOAT" */
var btype = "STANDARD";

/** Whether waypoint markers are currently displayed */
var waypointsToggle = false;

// ============================================================================
// INITIALIZATION
// ============================================================================

/**
 * Initialize balloon type selection and default mode.
 * Sets up event listeners for radio button changes and syncs visual state.
 */
$(document).ready(function() {
    // Handle balloon type radio button changes
    $('input[name="optradio"]').on('change', function () {
        const val = $(this).val();
        
        // Map radio button values to balloon types
        if (val === "standardbln") setMode("STANDARD");
        else if (val === "zpbbln") setMode("ZPB");
        else if (val === "floatbln") setMode("FLOAT");
        
        // Sync segmented control visual state (mobile UI)
        document.querySelectorAll('.segment').forEach(btn => {
            if (btn.getAttribute('data-mode') === val) {
                btn.classList.add('active');
            } else {
                btn.classList.remove('active');
            }
        });
    });

    // Initialize defaults and visibility on page load
    setMode("STANDARD");
});

/**
 * Initialize waypoint toggle button functionality.
 * 
 * Waypoints are circular markers shown along trajectory paths. This toggle
 * allows users to show/hide them. Button state is synced with waypointsToggle
 * global variable.
 */
$(function() {
    const waypointBtn = $('#waypoint-toggle');
    
    // Handle waypoint toggle button click
    waypointBtn.on('click', function(e) {
        waypointsToggle = !waypointsToggle;
        waypointBtn.toggleClass('on', waypointsToggle);
        
        // Show or hide waypoints based on toggle state
        if (waypointsToggle) {
            showWaypoints();
        } else {
            clearWaypoints();
        }
        
        // Blur button to clear active state on mobile (prevents sticky hover states)
        if (e.target) {
            e.target.blur();
        }
    });
    
    // Initialize button state and clear waypoints on page load
    waypointBtn.toggleClass('on', waypointsToggle);
    if (!waypointsToggle) {
        clearWaypoints();
    }
});

/**
 * Initialize date/time inputs with current date and time.
 * 
 * Sets both desktop and mobile time inputs to current values.
 * Also sets default values for ascent rate, burst altitude, and descent rate.
 */
var now = new Date(Date.now());

// Set desktop date/time inputs
document.getElementById("yr").value = now.getFullYear();
document.getElementById("mo").value = now.getMonth() + 1;
document.getElementById("day").value = now.getDate();
document.getElementById("hr").value = now.getHours();
document.getElementById("mn").value = now.getMinutes();

// Sync mobile time inputs (mobile UI has separate hour/minute inputs)
const hrMobile = document.getElementById("hr-mobile");
const mnMobile = document.getElementById("mn-mobile");
if (hrMobile) hrMobile.value = now.getHours();
if (mnMobile) mnMobile.value = now.getMinutes();

// Set default values for ascent rate, burst altitude, and descent rate
// Set on desktop inputs (mobile will sync via event listeners)
const ascEl = document.getElementById('asc');
const equilEl = document.getElementById('equil');
const descEl = document.getElementById('desc');
if (ascEl) ascEl.value = 4;        // Default ascent rate: 4 m/s
if (equilEl) equilEl.value = 30000; // Default burst altitude: 30000 m
if (descEl) descEl.value = 8;       // Default descent rate: 8 m/s

/**
 * Fetch current GEFS run identifier from server.
 * 
 * Displays the current model run timestamp (e.g., "2025110306") in the UI.
 * This indicates which weather model data is currently available.
 */
fetch(URL_ROOT + "/which").then(res => res.text()).then((result) => {
    const runElement = document.getElementById("run");
    if (runElement) {
        runElement.textContent = result;
    }
}).catch(err => {
    console.warn("Failed to fetch GEFS run identifier:", err);
});

/**
 * Fetch available model configuration from server.
 * 
 * Loads the list of available ensemble models and configuration options.
 * Falls back to default configuration if fetch fails.
 * 
 * Side effects:
 * - Sets window.availableModels array (e.g., [0, 1, 2, ..., 20])
 * - Sets window.modelConfig object with download_control and num_perturbed
 */
fetch(URL_ROOT + "/models").then(res => res.json()).then((config) => {
    window.availableModels = config.models;
    window.modelConfig = config;
    console.log("Available models:", config.models);
}).catch(err => {
    console.warn("Failed to fetch model config, using defaults:", err);
    // Fallback to default configuration (3 models: control + 2 perturbed)
    window.availableModels = [0, 1, 2];
    window.modelConfig = { download_control: true, num_perturbed: 2 };
});

/**
 * Update server status indicator in UI.
 * 
 * Fetches server status from backend and updates the status element with
 * appropriate color coding:
 * - "Ready" → Green (#00CC00)
 * - "Refreshing" → Yellow/Orange (#FFB900)
 * - Other/Error → Red (#CC0000)
 * 
 * Called initially on page load and then polled every 5 seconds.
 */
function updateServerStatus() {
    fetch(URL_ROOT + "/status")
        .then(res => res.text())
        .then((result) => {
            const el = document.getElementById("status");
            if (!el) return;
            
            el.textContent = result;
            
            // Color code based on status
            if (result === "Ready") {
                el.style.color = "#00CC00";  // Green
            } else if (result === "Refreshing") {
                el.style.color = "#FFB900";  // Yellow/Orange
            } else {
                el.style.color = "#CC0000";  // Red (error/unknown)
            }
        })
        .catch(() => {
            // Network error or server unavailable
            const el = document.getElementById("status");
            if (!el) return;
            el.textContent = "Unavailable";
            el.style.color = "#CC0000";  // Red
        });
}

// Initial status fetch and then poll every 5 seconds
updateServerStatus();
setInterval(updateServerStatus, 5000);

/**
 * Set default values for input fields ONLY if they are empty.
 * 
 * This preserves user's changes while ensuring fields have defaults on first load.
 * Called during initialization to populate empty fields with sensible defaults.
 * 
 * Default values:
 * - Ascent rate: 4 m/s
 * - Burst altitude: 30000 m
 * - Descent rate: 8 m/s
 * - Coefficient: 0.5 (FLOAT mode)
 * - Duration: 48 hours (FLOAT mode)
 * - Step: 120 seconds (FLOAT mode)
 * - Equilibrium time: 1 hour (ZPB mode)
 */
function setDefaultValues() {
    const ascEl = document.getElementById("asc");
    const equilEl = document.getElementById("equil");
    const descEl = document.getElementById("desc");
    const coeffEl = document.getElementById("coeff");
    const durEl = document.getElementById("dur");
    const stepEl = document.getElementById("step");
    const eqtimeEl = document.getElementById("eqtime");
    
    // Only set defaults if fields are empty (first load)
    // Check for empty string, null, or undefined
    if (ascEl && (!ascEl.value || ascEl.value.trim() === "")) ascEl.value = 4;
    if (equilEl && (!equilEl.value || equilEl.value.trim() === "")) equilEl.value = 30000;
    if (descEl && (!descEl.value || descEl.value.trim() === "")) descEl.value = 8;
    if (coeffEl && (!coeffEl.value || coeffEl.value.trim() === "")) coeffEl.value = 0.5;
    if (durEl && (!durEl.value || durEl.value.trim() === "")) durEl.value = 48;
    if (stepEl && (!stepEl.value || stepEl.value.trim() === "")) stepEl.value = 120;
    if (eqtimeEl && (!eqtimeEl.value || eqtimeEl.value.trim() === "")) eqtimeEl.value = 1;
}

/**
 * Initialize default values when DOM is ready.
 * 
 * Uses multiple strategies to ensure defaults are set even if DOM elements
 * aren't immediately available (handles race conditions with dynamic content).
 */
function initializeDefaults() {
    setDefaultValues();
    // Also try after a short delay in case elements aren't ready yet
    setTimeout(setDefaultValues, 100);
}

// Set defaults when DOM is ready (only on first load)
if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initializeDefaults);
} else {
    initializeDefaults();
}
/**
 * Switch balloon mode and update UI visibility accordingly.
 * 
 * Three modes are supported:
 * - STANDARD: Simple ascent/descent with burst altitude
 * - ZPB: Zero Pressure Balloon with equilibrium time
 * - FLOAT: Floating mode with coefficient, duration, and step size
 * 
 * @param {string} mode - Balloon type: "STANDARD", "ZPB", or "FLOAT"
 * 
 * Side effects:
 * - Updates global btype variable
 * - Shows/hides relevant input groups based on mode
 * - Sets default values for empty fields
 * - Updates timer visibility
 */
function setMode(mode) {
    btype = mode;
    
    // Get DOM elements for input groups and controls
    const geqtime = document.getElementById("group-eqtime");
    const gcoeff = document.getElementById("group-coeff");
    const gdur = document.getElementById("group-dur");
    const gstep = document.getElementById("group-step");
    const gtimer = document.getElementById("group-timeremain");
    const eqbtn = document.getElementById("eqtimebtn");
    const remain = document.getElementById("timeremain");
    
    /**
     * Helper: Set default value for a field if it's empty
     * @param {string} id - Element ID
     * @param {*} value - Default value to set
     */
    const setDefaultIfEmpty = (id, value) => {
        const el = document.getElementById(id);
        if (el && (!el.value || el.value.trim() === "")) {
            el.value = value;
        }
    };
    
    if (mode === "STANDARD") {
        // STANDARD mode: Simple ascent/descent, no equilibrium time
        if (geqtime) geqtime.style.display = "none";
        if (gcoeff) gcoeff.style.display = "none";
        if (gdur) gdur.style.display = "none";
        if (gstep) gstep.style.display = "none";
        if (gtimer) gtimer.style.display = "flex";
        if (eqbtn) eqbtn.style.visibility = "visible";
        setDefaultIfEmpty('asc', 4);
        setDefaultIfEmpty('equil', 30000);
        setDefaultIfEmpty('desc', 8);
        if (remain) remain.style.visibility = "visible";
        
    } else if (mode === "ZPB") {
        // ZPB mode: Zero Pressure Balloon with equilibrium time
        if (geqtime) geqtime.style.display = "flex";
        if (gcoeff) gcoeff.style.display = "none";
        if (gdur) gdur.style.display = "none";
        if (gstep) gstep.style.display = "none";
        if (gtimer) gtimer.style.display = "flex";
        if (eqbtn) eqbtn.style.visibility = "visible";
        setDefaultIfEmpty('asc', 4);
        setDefaultIfEmpty('equil', 30000);
        setDefaultIfEmpty('desc', 8);
        setDefaultIfEmpty('eqtime', 1);
        if (remain) remain.style.visibility = "visible";
        
    } else { // FLOAT
        // FLOAT mode: Floating with coefficient, duration, and step size
        if (geqtime) geqtime.style.display = "none";
        if (gcoeff) gcoeff.style.display = "flex";
        if (gdur) gdur.style.display = "flex";
        if (gstep) gstep.style.display = "flex";
        if (gtimer) gtimer.style.display = "none";
        if (eqbtn) eqbtn.style.visibility = "hidden";
        setDefaultIfEmpty('asc', 4);
        setDefaultIfEmpty('equil', 30000);
        setDefaultIfEmpty('desc', 8);
        setDefaultIfEmpty('coeff', 0.5);
        setDefaultIfEmpty('dur', 48);
        setDefaultIfEmpty('step', 120);
        if (remain) remain.style.visibility = "hidden";
    }
}
