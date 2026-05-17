(function () {
    function escapeHtml(value) {
        return String(value)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;');
    }

    function summarizeClosure(closure) {
        if (!closure || typeof closure !== 'object') {
            return null;
        }
        const spec = closure.closure_spec || {};
        const gates = spec.gates || {};
        const verificationRun = closure.last_verification_run || {};
        const results = verificationRun.results_json || {};
        const criticalFlows = Array.isArray(spec.critical_flows) ? spec.critical_flows : [];
        const passingFlows = criticalFlows.filter((flow) => {
            const flowId = flow && flow.id;
            return flowId && results.critical_flows && results.critical_flows[flowId] === true;
        }).length;
        const requiredFlows = Math.max(
            criticalFlows.length,
            Number(gates.critical_flow_count || 0)
        );
        const maxOpenRegressions = Number(gates.max_open_regressions || 0);
        return {
            mode: closure.closure_mode || 'build',
            status: closure.closure_status || 'yellow',
            openRegressions: Number(closure.open_regression_count || 0),
            stallCount: Number(closure.stall_count || 0),
            maxOpenRegressions,
            bootOk: results.boot_ok,
            testsOk: results.tests_ok,
            flowsPassing: passingFlows,
            flowsRequired: requiredFlows,
            lastVerificationStatus: closure.last_verification_status || verificationRun.status || 'never',
        };
    }

    function renderGatePill(label, value, goodWhenTrue = true) {
        let color = '#8b949e';
        if (value === true) color = goodWhenTrue ? '#3fb950' : '#f85149';
        if (value === false) color = goodWhenTrue ? '#f85149' : '#3fb950';
        return `<span style="display:inline-flex;align-items:center;gap:4px;padding:2px 8px;border:1px solid ${color};border-radius:999px;font-size:11px;color:${color}">${escapeHtml(label)}</span>`;
    }

    function renderKeyValueList(items) {
        if (!items.length) {
            return '<div style="font-size:11px;color:#8b949e">none</div>';
        }
        return `<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:6px 12px;font-size:12px">` +
            items.map(({label, value}) => `
                <div>
                    <div style="color:#8b949e">${escapeHtml(label)}</div>
                    <div style="color:#e6edf3;word-break:break-word">${escapeHtml(String(value))}</div>
                </div>
            `).join('') +
            `</div>`;
    }

    function renderProjectClosureSummary(closure, proposal) {
        const summary = summarizeClosure(closure);
        if (!summary) return '';
        const proposedSpec = proposal && proposal.closure_spec && typeof proposal.closure_spec === 'object'
            ? proposal.closure_spec
            : null;
        const proposalFlow = proposedSpec && Array.isArray(proposedSpec.critical_flows) && proposedSpec.critical_flows.length
            ? proposedSpec.critical_flows[0]
            : null;
        const proposalLabel = proposal && proposal.source
            ? `${proposal.source} · ${proposal.profile || 'unknown'}`
            : 'not loaded';

        const statusColors = {
            green: '#3fb950',
            yellow: '#d29922',
            red: '#f85149',
            frozen: '#58a6ff',
            stalled: '#ff7b72',
            build: '#8b949e',
        };
        const statusColor = statusColors[summary.status] || '#8b949e';
        const flowLabel = summary.flowsRequired > 0
            ? `${summary.flowsPassing}/${summary.flowsRequired} flows`
            : 'no flows';
        const projectName = closure.project || '';
        const escapedProject = escapeHtml(projectName).replace(/'/g, "\\'");
        const editorId = `closure-editor-${projectName.replace(/[^a-z0-9_-]/gi, '_')}`;
        const detailsId = `closure-live-${projectName.replace(/[^a-z0-9_-]/gi, '_')}`;
        const spec = closure.closure_spec && typeof closure.closure_spec === 'object' ? closure.closure_spec : {};
        const boot = spec.boot && typeof spec.boot === 'object' ? spec.boot : {};
        const readyCheck = boot.ready_check && typeof boot.ready_check === 'object' ? boot.ready_check : {};
        const verification = spec.verification && typeof spec.verification === 'object' ? spec.verification : {};
        const smokeChecks = Array.isArray(verification.smoke_checks) ? verification.smoke_checks : [];
        const criticalFlows = Array.isArray(spec.critical_flows) ? spec.critical_flows : [];
        const gates = spec.gates && typeof spec.gates === 'object' ? spec.gates : {};
        const autonomy = spec.autonomy && typeof spec.autonomy === 'object' ? spec.autonomy : {};
        const liveSpecJson = escapeHtml(JSON.stringify(spec, null, 2));
        const modeButtons = ['build', 'stabilize', 'ship'].map((mode) => {
            const active = summary.mode === mode;
            const color = active ? '#58a6ff' : '#8b949e';
            return `<button type="button" onclick="setClosureMode(event,'${escapedProject}','${mode}')"
                style="background:transparent;color:${color};border:1px solid ${color};border-radius:999px;padding:2px 8px;font-size:11px;cursor:pointer">
                ${escapeHtml(mode)}
            </button>`;
        }).join('');

        return `
            <div class="closure-summary" style="margin-top:10px;padding:10px 12px;border:1px solid #30363d;border-radius:8px;background:#11161d">
                <div style="display:flex;justify-content:space-between;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:8px">
                    <div style="font-size:11px;color:#8b949e;text-transform:uppercase;letter-spacing:.08em">Closure</div>
                    <div style="display:flex;gap:6px;align-items:center;flex-wrap:wrap">
                        <span style="display:inline-flex;align-items:center;padding:2px 8px;border:1px solid ${statusColor};border-radius:999px;font-size:11px;color:${statusColor}">
                            ${escapeHtml(summary.status)}
                        </span>
                        <span style="font-size:11px;color:#8b949e">mode ${escapeHtml(summary.mode)}</span>
                    </div>
                </div>
                <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:8px;font-size:12px;margin-bottom:8px">
                    <div><span style="color:#8b949e">Regressions</span><div style="color:#e6edf3">${summary.openRegressions}/${summary.maxOpenRegressions}</div></div>
                    <div><span style="color:#8b949e">Stall Count</span><div style="color:#e6edf3">${summary.stallCount}</div></div>
                    <div><span style="color:#8b949e">Flows</span><div style="color:#e6edf3">${escapeHtml(flowLabel)}</div></div>
                    <div><span style="color:#8b949e">Verify</span><div style="color:#e6edf3">${escapeHtml(summary.lastVerificationStatus)}</div></div>
                </div>
                <div style="display:flex;gap:6px;flex-wrap:wrap">
                    ${renderGatePill(`boot ${summary.bootOk === true ? 'ok' : summary.bootOk === false ? 'fail' : 'n/a'}`, summary.bootOk)}
                    ${renderGatePill(`tests ${summary.testsOk === true ? 'ok' : summary.testsOk === false ? 'fail' : 'n/a'}`, summary.testsOk)}
                    ${renderGatePill(`flows ${flowLabel}`, summary.flowsRequired === 0 ? null : summary.flowsPassing >= summary.flowsRequired)}
                    ${renderGatePill(`regressions ${summary.openRegressions}`, summary.openRegressions <= summary.maxOpenRegressions)}
                </div>
                <details id="${escapeHtml(detailsId)}" style="margin-top:10px;padding:8px 10px;border:1px solid #21262d;border-radius:6px;background:#0d1117">
                    <summary style="cursor:pointer;color:#e6edf3;font-size:12px">Live Contract</summary>
                    <div style="margin-top:10px">
                        <div style="font-size:11px;color:#8b949e;text-transform:uppercase;letter-spacing:.08em;margin-bottom:6px">Boot</div>
                        ${renderKeyValueList([
                            {label: 'command', value: boot.command || 'none'},
                            {label: 'cwd', value: boot.cwd || 'default'},
                            {label: 'ready_check.type', value: readyCheck.type || 'none'},
                            {label: 'ready_check.target', value: readyCheck.command || readyCheck.url || readyCheck.target || readyCheck.scene || 'none'},
                        ])}
                    </div>
                    <div style="margin-top:10px">
                        <div style="font-size:11px;color:#8b949e;text-transform:uppercase;letter-spacing:.08em;margin-bottom:6px">Verification</div>
                        ${renderKeyValueList([
                            {label: 'unit_test_command', value: verification.unit_test_command || 'none'},
                            {label: 'integration_test_command', value: verification.integration_test_command || 'none'},
                            {label: 'smoke_checks', value: smokeChecks.length ? smokeChecks.map((check) => `${check.id || 'check'}:${check.type || 'unknown'}`).join(', ') : 'none'},
                        ])}
                    </div>
                    <div style="margin-top:10px">
                        <div style="font-size:11px;color:#8b949e;text-transform:uppercase;letter-spacing:.08em;margin-bottom:6px">Critical Flows</div>
                        ${criticalFlows.length
                            ? criticalFlows.map((flow) => `<div style="font-size:12px;color:#e6edf3;margin-bottom:4px"><strong>${escapeHtml(flow.id || 'flow')}</strong> <span style="color:#8b949e">${escapeHtml(flow.description || '')}</span></div>`).join('')
                            : '<div style="font-size:11px;color:#8b949e">none</div>'}
                    </div>
                    <div style="margin-top:10px">
                        <div style="font-size:11px;color:#8b949e;text-transform:uppercase;letter-spacing:.08em;margin-bottom:6px">Gates</div>
                        ${renderKeyValueList(Object.entries(gates).map(([label, value]) => ({label, value})))}
                    </div>
                    <div style="margin-top:10px">
                        <div style="font-size:11px;color:#8b949e;text-transform:uppercase;letter-spacing:.08em;margin-bottom:6px">Autonomy</div>
                        ${renderKeyValueList(Object.entries(autonomy).map(([label, value]) => ({label, value})))}
                    </div>
                </details>
                <details style="margin-top:10px;padding:8px 10px;border:1px solid #21262d;border-radius:6px;background:#0d1117">
                    <summary style="cursor:pointer;color:#e6edf3;font-size:12px">Edit Live Contract JSON</summary>
                    <div style="margin-top:10px;font-size:11px;color:#8b949e">
                        This edits the structured closure spec the controller actually uses.
                    </div>
                    <textarea id="${escapeHtml(editorId)}" rows="16"
                        style="width:100%;margin-top:8px;background:#0b0f14;color:#e6edf3;border:1px solid #30363d;border-radius:6px;padding:8px;font-size:12px;resize:vertical;box-sizing:border-box;font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace">${liveSpecJson}</textarea>
                    <div style="display:flex;gap:6px;flex-wrap:wrap;margin-top:8px">
                        <button type="button" onclick="copyClosureSpec(event,'${escapedProject}','${escapeHtml(editorId)}')"
                            style="background:transparent;color:#8b949e;border:1px solid #8b949e;border-radius:4px;padding:2px 8px;font-size:11px;cursor:pointer">
                            Copy JSON
                        </button>
                        <button type="button" onclick="saveClosureSpec(event,'${escapedProject}','${escapeHtml(editorId)}')"
                            style="background:transparent;color:#3fb950;border:1px solid #3fb950;border-radius:4px;padding:2px 8px;font-size:11px;cursor:pointer">
                            Save Contract
                        </button>
                    </div>
                </details>
                <div style="margin-top:10px;padding:8px 10px;border:1px solid #21262d;border-radius:6px;background:#0d1117">
                    <div style="display:flex;justify-content:space-between;align-items:center;gap:8px;flex-wrap:wrap">
                        <div>
                            <div style="font-size:11px;color:#8b949e;text-transform:uppercase;letter-spacing:.08em">Proposed Contract</div>
                            <div style="font-size:12px;color:#e6edf3">${escapeHtml(proposalLabel)}</div>
                            <div style="font-size:11px;color:#8b949e;margin-top:2px">
                                ${proposalFlow ? escapeHtml(proposalFlow.id || 'main-flow') : 'Preview a proposal to inspect the representative flow.'}
                            </div>
                        </div>
                        <div style="display:flex;gap:6px;flex-wrap:wrap">
                            <button type="button" onclick="previewClosureProposal(event,'${escapedProject}')"
                                style="background:transparent;color:#58a6ff;border:1px solid #58a6ff;border-radius:4px;padding:2px 8px;font-size:11px;cursor:pointer">
                                ♻ Preview Proposal
                            </button>
                            <button type="button" onclick="applyClosureProposal(event,'${escapedProject}')"
                                style="background:transparent;color:#3fb950;border:1px solid #3fb950;border-radius:4px;padding:2px 8px;font-size:11px;cursor:pointer">
                                ⤴ Apply Proposal
                            </button>
                        </div>
                    </div>
                </div>
                <div style="display:flex;justify-content:space-between;align-items:center;gap:8px;flex-wrap:wrap;margin-top:10px;padding-top:10px;border-top:1px solid #21262d">
                    <div style="display:flex;gap:6px;flex-wrap:wrap">
                        <button type="button" onclick="verifyProjectClosure(event,'${escapedProject}')"
                            style="background:transparent;color:#3fb950;border:1px solid #3fb950;border-radius:4px;padding:2px 8px;font-size:11px;cursor:pointer">
                            ✓ Verify Now
                        </button>
                        <button type="button" onclick="queueClosureRepair(event,'${escapedProject}')"
                            style="background:transparent;color:#e3b341;border:1px solid #e3b341;border-radius:4px;padding:2px 8px;font-size:11px;cursor:pointer">
                            🔧 Generate Repair
                        </button>
                    </div>
                    <div style="display:flex;gap:6px;flex-wrap:wrap">
                        ${modeButtons}
                    </div>
                </div>
            </div>
        `;
    }

    window.SwarmClosureUI = {
        renderProjectClosureSummary,
    };
})();
