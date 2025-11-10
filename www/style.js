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
    waypointBtn.on('click', function() {
        waypointsToggle = !waypointsToggle;
        waypointBtn.toggleClass('on', waypointsToggle);
        if (waypointsToggle) {
            showWaypoints();
        } else {
            clearWaypoints();
        }
    });
    waypointBtn.toggleClass('on', waypointsToggle);
    if (!waypointsToggle) {
        clearWaypoints();
    }
});

var now = new Date(Date.now());
document.getElementById("yr").value = now.getUTCFullYear()
document.getElementById("mo").value = now.getUTCMonth() + 1
document.getElementById("day").value = now.getUTCDate()
document.getElementById("hr").value = now.getUTCHours()
document.getElementById("mn").value = now.getUTCMinutes()
// Sync mobile time inputs
const hrMobile = document.getElementById("hr-mobile");
const mnMobile = document.getElementById("mn-mobile");
if(hrMobile) hrMobile.value = now.getUTCHours();
if(mnMobile) mnMobile.value = now.getUTCMinutes();

/*document.getElementById("yr").value = 2020
document.getElementById("mo").value = 9
document.getElementById("day").value = 23
document.getElementById("hr").value = 12
document.getElementById("mn").value = 00*/

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
document.getElementById("asc").value = 4;
document.getElementById("equil").value = 30000;
document.getElementById("desc").value = 8;
document.getElementById("coeff").value = 0.5;
document.getElementById("dur").value = 48;
document.getElementById("step").value = 240;
document.getElementById("eqtime").value = 1;


// Missions UI removed

function setMode(mode){
    btype = mode;
    // groups
    var geqtime = document.getElementById("group-eqtime");
    var gcoeff = document.getElementById("group-coeff");
    var gdur = document.getElementById("group-dur");
    var gstep = document.getElementById("group-step");
    var gtimer = document.getElementById("group-timeremain");
    // buttons
    var eqbtn = document.getElementById("eqtimebtn");

    if (mode === "STANDARD"){
        geqtime.style.display = "none";
        gcoeff.style.display = "none";
        gdur.style.display = "none";
        gstep.style.display = "none";
        if (gtimer) gtimer.style.display = "flex";
        if (eqbtn) eqbtn.style.visibility = "visible";
        document.getElementById("asc").value = 4;
        document.getElementById("equil").value = 30000;
        document.getElementById("desc").value = 8;
        var remain = document.getElementById("timeremain");
        if (remain) remain.style.visibility = "visible";
    } else if (mode === "ZPB"){
        geqtime.style.display = "flex";
        gcoeff.style.display = "none";
        gdur.style.display = "none";
        gstep.style.display = "none";
        if (gtimer) gtimer.style.display = "flex";
        if (eqbtn) eqbtn.style.visibility = "visible";
        document.getElementById("asc").value = 4;
        document.getElementById("equil").value = 30000;
        document.getElementById("eqtime").value = 1;
        document.getElementById("desc").value = 8;
        var remain = document.getElementById("timeremain");
        if (remain) remain.style.visibility = "visible";
    } else { // FLOAT
        geqtime.style.display = "none";
        gcoeff.style.display = "flex";
        gdur.style.display = "flex";
        gstep.style.display = "flex";
        if (gtimer) gtimer.style.display = "none";
        if (eqbtn) eqbtn.style.visibility = "hidden";
        document.getElementById("asc").value = 4;
        document.getElementById("equil").value = 30000;
        document.getElementById("desc").value = 8;
        document.getElementById("coeff").value = 0.5;
        document.getElementById("dur").value = 48;
        document.getElementById("step").value = 240;
        var remain = document.getElementById("timeremain");
        if (remain) remain.style.visibility = "hidden";
    }
}
