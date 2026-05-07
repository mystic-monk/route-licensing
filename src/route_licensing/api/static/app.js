// ====================================================================
// Route Licensing — Client-Side JavaScript
// ====================================================================

document.addEventListener('DOMContentLoaded', () => {
    initUploadZone();
    initExpandableRows();
});

// --- Drag & Drop Upload ---
function initUploadZone() {
    const zone = document.getElementById('upload-zone');
    const input = document.getElementById('file-input');
    const form = document.getElementById('upload-form');

    if (!zone || !input || !form) return;

    // Click to browse — block if operator name is not filled
    zone.addEventListener('click', () => {
        const operatorInput = document.getElementById('operator-input');
        const operatorVal = operatorInput ? operatorInput.value.trim() : '';
        if (!operatorVal) {
            showToast('Please enter the Operator Name before uploading.', 'error');
            if (operatorInput) operatorInput.focus();
            return;
        }
        input.click();
    });

    // Drag events
    ['dragenter', 'dragover'].forEach(evt => {
        zone.addEventListener(evt, (e) => {
            e.preventDefault();
            zone.classList.add('drag-over');
        });
    });

    ['dragleave', 'drop'].forEach(evt => {
        zone.addEventListener(evt, (e) => {
            e.preventDefault();
            zone.classList.remove('drag-over');
        });
    });

    zone.addEventListener('drop', (e) => {
        const files = e.dataTransfer.files;
        if (files.length > 0) {
            input.files = files;
            handleFileSelected(files[0], form);
        }
    });

    // Regular file selection
    input.addEventListener('change', () => {
        if (input.files.length > 0) {
            handleFileSelected(input.files[0], form);
        }
    });
}

function handleFileSelected(file, form) {
    // Validate file type
    const validExts = ['.xlsx', '.xls'];
    const ext = file.name.substring(file.name.lastIndexOf('.'));
    if (!validExts.includes(ext.toLowerCase())) {
        showToast('Please upload an Excel file (.xlsx or .xls)', 'error');
        return;
    }

    // Validate operator field before starting any animation
    const operatorInput = document.getElementById('operator-input');
    const operatorVal = operatorInput ? operatorInput.value.trim() : '';
    if (!operatorVal) {
        showToast('Please enter the Operator Name before uploading.', 'error');
        if (operatorInput) operatorInput.focus();
        // Reset the file input so the same file can be selected again after the user fills in the operator name
        const fileInput = document.getElementById('file-input');
        if (fileInput) fileInput.value = '';
        return;
    }

    // Show progress
    const progress = document.getElementById('upload-progress');
    const progressFill = document.querySelector('.progress-bar-fill');
    const progressText = document.querySelector('.progress-text');
    const uploadIcon = document.querySelector('.upload-icon');
    const uploadTitle = document.querySelector('.upload-zone h3');

    if (progress) progress.classList.add('active');
    if (uploadIcon) uploadIcon.textContent = '⏳';
    if (uploadTitle) uploadTitle.textContent = `Analysing ${file.name}...`;

    // Animate progress
    let progressValue = 0;
    const progressInterval = setInterval(() => {
        progressValue = Math.min(progressValue + Math.random() * 15, 90);
        if (progressFill) progressFill.style.width = progressValue + '%';
        if (progressText) {
            if (progressValue < 30) progressText.textContent = 'Parsing timetable...';
            else if (progressValue < 60) progressText.textContent = 'Comparing against GTFS services...';
            else progressText.textContent = 'Generating assessment...';
        }
    }, 400);

    // Submit via fetch
    const formData = new FormData();
    formData.append('file', file);
    formData.append('operator', operatorVal);

    fetch('/api/v1/analyze', {
        method: 'POST',
        body: formData,
    })
    .then(response => {
        clearInterval(progressInterval);
        if (!response.ok) {
            return response.json().then(d => { throw new Error(d.detail || 'Analysis failed'); });
        }
        return response.json();
    })
    .then(data => {
        if (progressFill) progressFill.style.width = '100%';
        if (progressText) progressText.textContent = 'Complete! Redirecting...';
        showToast('Analysis complete!', 'success');

        // Redirect to results page
        setTimeout(() => {
            window.location.href = `/results/${data.route_id}`;
        }, 800);
    })
    .catch(err => {
        clearInterval(progressInterval);
        if (progress) progress.classList.remove('active');
        if (uploadIcon) uploadIcon.textContent = '📄';
        if (uploadTitle) uploadTitle.textContent = 'Drop your Excel timetable here';
        showToast(err.message || 'Analysis failed. Please try again.', 'error');
    });
}

// --- Expandable Table Rows ---
function initExpandableRows() {
    document.querySelectorAll('.expand-toggle').forEach(toggle => {
        toggle.addEventListener('click', () => {
            const targetId = toggle.dataset.target;
            const detailRow = document.getElementById(targetId);
            if (!detailRow) return;

            const isOpen = toggle.classList.contains('open');

            // Close all first
            document.querySelectorAll('.expand-toggle.open').forEach(t => {
                t.classList.remove('open');
            });
            document.querySelectorAll('.detail-row.open').forEach(r => {
                r.classList.remove('open');
            });

            // Toggle clicked
            if (!isOpen) {
                toggle.classList.add('open');
                detailRow.classList.add('open');
            }
        });
    });
}

// --- Toast Notifications ---
function showToast(message, type = 'success') {
    // Remove existing
    const existing = document.querySelector('.toast');
    if (existing) existing.remove();

    const toast = document.createElement('div');
    toast.className = `toast ${type}`;
    toast.textContent = message;
    document.body.appendChild(toast);

    requestAnimationFrame(() => {
        toast.classList.add('show');
    });

    setTimeout(() => {
        toast.classList.remove('show');
        setTimeout(() => toast.remove(), 300);
    }, 4000);
}

// --- Risk Score Pips ---
function renderRiskPips(score, maxScore) {
    let html = '<div class="risk-bar">';
    for (let i = 0; i < maxScore; i++) {
        const filled = i < score;
        let level = 'low';
        if (score >= 4) level = 'high';
        else if (score >= 2) level = 'mid';
        html += `<div class="risk-pip ${filled ? 'filled ' + level : ''}"></div>`;
    }
    html += `<span class="risk-value">${score}</span></div>`;
    return html;
}
