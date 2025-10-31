var btype = "STANDARD"
$(document).ready(function() {
    $('input[name="optradio"]').on('change', function () {
        if ($(this).val() === "standardbln") setMode("STANDARD");
        else if ($(this).val() === "zpbbln") setMode("ZPB");
        else if ($(this).val() === "floatbln") setMode("FLOAT");
    });

    // initialize defaults and visibility
    setMode("STANDARD");
});

var waypointsToggle = true;
$(function() {
    $('#toggle-event').change(function() {
        var state = $(this).prop('checked');
        if(!state){
            waypointsToggle = false
            clearWaypoints();
        }
        else{
            waypointsToggle = true;
            showWaypoints()
        }
    })
});

var now = new Date(Date.now());
document.getElementById("yr").value = now.getUTCFullYear()
document.getElementById("mo").value = now.getUTCMonth() + 1
document.getElementById("day").value = now.getUTCDate()
document.getElementById("hr").value = now.getUTCHours()
document.getElementById("mn").value = now.getUTCMinutes()

/*document.getElementById("yr").value = 2020
document.getElementById("mo").value = 9
document.getElementById("day").value = 23
document.getElementById("hr").value = 12
document.getElementById("mn").value = 00*/

fetch(URL_ROOT + "/which").then(res => res.text()).then((result) => {
            document.getElementById("run").textContent = result
        });

fetch(URL_ROOT + "/status").then(res => res.text()).then((result) => {
    document.getElementById("status").textContent = result;
    if(result === "Ready") {
        document.getElementById("status").style.color = "#00CC00";
    }
    else if(result === "Data refreshing. Sims may be slower than usual."){
        document.getElementById("status").style.color = "#FFB900";
    }
    else{
        document.getElementById("status").style.color = "#CC0000";
    }});

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
    // buttons
    var eqbtn = document.getElementById("eqtimebtn");

    if (mode === "STANDARD"){
        geqtime.style.visibility = "hidden";
        gcoeff.style.visibility = "hidden";
        gdur.style.visibility = "hidden";
        gstep.style.visibility = "hidden";
        eqbtn.style.visibility = "visible";
        document.getElementById("asc").value = 4;
        document.getElementById("equil").value = 30000;
        document.getElementById("desc").value = 8;
    } else if (mode === "ZPB"){
        geqtime.style.visibility = "visible";
        gcoeff.style.visibility = "hidden";
        gdur.style.visibility = "hidden";
        gstep.style.visibility = "hidden";
        eqbtn.style.visibility = "visible";
        document.getElementById("asc").value = 3.7;
        document.getElementById("equil").value = 29000;
        document.getElementById("eqtime").value = 1;
        document.getElementById("desc").value = 15;
    } else { // FLOAT
        geqtime.style.visibility = "hidden";
        gcoeff.style.visibility = "visible";
        gdur.style.visibility = "visible";
        gstep.style.visibility = "visible";
        eqbtn.style.visibility = "hidden";
        document.getElementById("coeff").value = 0.5;
        document.getElementById("dur").value = 48;
        document.getElementById("step").value = 240;
        document.getElementById("timeremain").style.visibility = "hidden";
    }
}
