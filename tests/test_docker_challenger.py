import subprocess
import os
import sys

def test_vulnerability_scan():
    log_dir = "/Users/thiagoanselmobarbosa/Desktop/medflow full/.agents/challenger_docker_gen2_2"
    os.makedirs(log_dir, exist_ok=True)
    
    project_dir = "/Users/thiagoanselmobarbosa/Desktop/medflow full/CareFlow AI/careflow-backend"
    scanner_script = "/Users/thiagoanselmobarbosa/.gemini/config/skills/vulnerability-scanner/scripts/security_scan.py"
    
    with open(os.path.join(log_dir, "security_scan_results.json"), "w") as f:
        # Run python security_scan.py <project_path>
        res = subprocess.run([sys.executable, scanner_script, project_dir], capture_output=True, text=True)
        f.write(res.stdout)
        
        # Write stderr to another file if any
        if res.stderr:
            with open(os.path.join(log_dir, "security_scan_errors.txt"), "w") as ferr:
                ferr.write(res.stderr)
