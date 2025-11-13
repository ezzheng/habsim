var btype = "STANDARD"
$(document).ready(function() {
    $('input[name="optradio"]').on('change', function () {
        if ($(this).val() === "standardbln") setMode("STANDARD");
        else if ($(this).val() === "zpbbln") setMode("ZPB");
        else if ($(this).val() === "floatbln") setMode("FLOAT");
        // sync segmented control visual state
        const val = $(this).val();
        document.querySelectorAll('.segment').forEach(btn => {
            if (btn.getAttribute('data-mode') === val) btn.classList.add('active');
            else btn.classList.remove('active');
        });
    });

    // initialize defaults and visibility
    setMode("STANDARD");
});

var waypointsToggle = false;
$(function() {
    const waypointBtn = $('#waypoint-toggle');
    waypointBtn.on('click', function(e) {
        waypointsToggle = !waypointsToggle;
        waypointBtn.toggleClass('on', waypointsToggle);
        if (waypointsToggle) {
            showWaypoints();
        } else {
            clearWaypoints();
        }
        // Blur button to clear active state on mobile
        if (e.target) {
            e.target.blur();
        }
    });
    waypointBtn.toggleClass('on', waypointsToggle);
    if (!waypointsToggle) {
        clearWaypoints();
    }
});

var now = new Date(Date.now());
document.getElementById("yr").value = now.getFullYear()
document.getElementById("mo").value = now.getMonth() + 1
document.getElementById("day").value = now.getDate()
document.getElementById("hr").value = now.getHours()
document.getElementById("mn").value = now.getMinutes()
// Sync mobile time inputs
const hrMobile = document.getElementById("hr-mobile");
const mnMobile = document.getElementById("mn-mobile");
if(hrMobile) hrMobile.value = now.getHours();
if(mnMobile) mnMobile.value = now.getMinutes();

// Set default values for ascent rate, burst altitude, and descent rate (same way as Date/Time)
// Set on desktop inputs (mobile will sync via event listeners)
const ascEl = document.getElementById('asc');
const equilEl = document.getElementById('equil');
const descEl = document.getElementById('desc');
if (ascEl) ascEl.value = 4;
if (equilEl) equilEl.value = 30000;
if (descEl) descEl.value = 8;

fetch(URL_ROOT + "/which").then(res => res.text()).then((result) => {
            document.getElementById("run").textContent = result
        });

// Fetch available model configuration from server
fetch(URL_ROOT + "/models").then(res => res.json()).then((config) => {
    window.availableModels = config.models;
    window.modelConfig = config;
    console.log("Available models:", config.models);
}).catch(err => {
    console.warn("Failed to fetch model config, using defaults:", err);
    // Fallback to default configuration
    window.availableModels = [0, 1, 2];
    window.modelConfig = { download_control: true, num_perturbed: 2 };
});

function updateServerStatus() {
    fetch(URL_ROOT + "/status")
        .then(res => res.text())
        .then((result) => {
            const el = document.getElementById("status");
            if (!el) return;
            el.textContent = result;
    if(result === "Ready") {
                el.style.color = "#00CC00";
    }
            else if(result === "Refreshing"){
                el.style.color = "#FFB900";
    }
    else{
                el.style.color = "#CC0000";
            }
        })
        .catch(() => {
            const el = document.getElementById("status");
            if (!el) return;
            el.textContent = "Unavailable";
            el.style.color = "#CC0000";
        });
}

// Initial status fetch and then poll every 5s
updateServerStatus();
setInterval(updateServerStatus, 5000);

// We need to keep this because standard code does not execute until you choose the button
// Set defaults ONLY on initial page load - don't reset user's changes
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

// Set defaults when DOM is ready (only on first load)
// Use multiple strategies to ensure it runs
function initializeDefaults() {
    setDefaultValues();
    // Also try after a short delay in case elements aren't ready yet
    setTimeout(setDefaultValues, 100);
}

if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initializeDefaults);
} else {
    initializeDefaults();
}
function setMode(mode) {
    btype = mode;
    const geqtime = document.getElementById("group-eqtime");
    const gcoeff = document.getElementById("group-coeff");
    const gdur = document.getElementById("group-dur");
    const gstep = document.getElementById("group-step");
    const gtimer = document.getElementById("group-timeremain");
    const eqbtn = document.getElementById("eqtimebtn");
    const remain = document.getElementById("timeremain");
    
    const setDefaultIfEmpty = (id, value) => {
        const el = document.getElementById(id);
        if (el && (!el.value || el.value.trim() === "")) el.value = value;
    };
    
    if (mode === "STANDARD") {
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
