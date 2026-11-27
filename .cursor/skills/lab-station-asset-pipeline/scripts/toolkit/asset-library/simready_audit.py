"""Run the actual installed NVIDIA validator; never infer certification from CLI exit alone."""
import sys
sys.dont_write_bytecode=True
import argparse,subprocess,json,ast,tomllib
from pathlib import Path
from core import read,write,digest,inside
from inspect_usd import inspect

def evaluate_report(details, required, expected_profile, expected_version, exit_code):
    """NVIDIA hard/soft verdict plus strict identity/completeness safeguards.

    This matches installed is_blocking_failure for well-formed summaries while
    retaining the distinct, stricter all-direct-required-features-pass result.
    """
    features=details.get('features_summary',{})
    errors=[];blocking=[]
    if not isinstance(features,dict) or not features:
        errors.append('empty_or_invalid_features_summary');features={}
    if details.get('profile_id')!=expected_profile:errors.append('profile_identity_mismatch')
    if details.get('profile_version')!=expected_version:errors.append('profile_version_mismatch')
    if not details.get('simready_validate_version'):errors.append('missing_validator_identity')
    if exit_code!=0:errors.append('validator_process_failed')
    missing=[k for k in required if k not in features]
    if missing:errors.append('missing_required_features')
    for key,entry in features.items():
        if not isinstance(entry,dict) or not isinstance(entry.get('passed'),bool):
            errors.append('malformed_feature:'+key);blocking.append(key);continue
        if 'blocking_failure' in entry and not isinstance(entry['blocking_failure'],bool):
            errors.append('invalid_blocking_flag:'+key);blocking.append(key);continue
        if entry['passed']:
            if entry.get('blocking_failure') or entry.get('failing requirements'):
                errors.append('contradictory_feature:'+key);blocking.append(key)
            continue
        if 'blocking_failure' in entry:
            is_blocking=entry['blocking_failure']
            if not is_blocking and 'failing requirements' in entry:
                errors.append('contradictory_blocking_flag:'+key);is_blocking=True
        else:
            is_blocking='failing requirements' in entry or 'optional requirements' not in entry
        if is_blocking:blocking.append(key)
    failed_direct=[k for k in required if k in features and (not isinstance(features[k],dict) or features[k].get('passed') is not True)]
    return {'official_profile_passed':bool(features) and not errors and not blocking,
            'direct_required_full_passed':bool(features) and not errors and not missing and not failed_direct,
            'blocking_features':blocking,'validation_contract_errors':errors,
            'missing_features':missing,'failed_required_features':failed_direct}

def audit_usd(usd, output, validator, profiles, profile='Robot-Body', version=None, timeout=240):
    """Validate one working derivative without mutating any catalog or sealed input."""
    usd=Path(usd).resolve();output=Path(output).resolve()
    output.mkdir(parents=True,exist_ok=False)
    names={'Robot-Body':'robot_body.toml','Robot-Gripper':'robot_gripper.toml','Robotics-Prop':'robotics_prop.toml'}
    definition_path=Path(profiles)/names[profile]
    definitions=tomllib.loads(definition_path.read_text(encoding='utf8'))[profile]
    version=version or max(definitions,key=lambda x:tuple(map(int,x.split('.'))))
    required=[next(iter(f)) for f in definitions[version]['features'] if not f.get('optional',False)]
    raw=output/'nvidia-validation.json';h=digest(usd)
    command=[str(validator),'--profile',profile,'--version',version,'--output',str(raw),str(usd)]
    write(output/'invocation.json',{'command':command,'input_sha256':h,'profile_definition_sha256':digest(definition_path)})
    try:
        process=subprocess.run(command,capture_output=True,timeout=timeout)
        (output/'stdout.log').write_bytes(process.stdout);(output/'stderr.log').write_bytes(process.stderr)
        data=read(raw) if raw.exists() else {};details=next(iter(data.values()),{})
        verdict=evaluate_report(details,required,profile,version,process.returncode)
        if digest(usd)!=h:raise ValueError('Validator changed input')
        features=details.get('features_summary',{});issues={}
        for key,feature in features.items():
            for requirement in ast.literal_eval(feature.get('failing requirements','[]')):
                issues.setdefault(requirement,[]).append(key)
        summary={'status':'passed' if verdict['official_profile_passed'] and verdict['direct_required_full_passed'] else 'failed',
                 **verdict,'input_sha256':h,'profile':profile,'profile_version':version,'required_features':required,
                 'validator_version':details.get('simready_validate_version'),'exit_code':process.returncode,
                 'profile_definition_sha256':digest(definition_path),'raw_report_sha256':digest(raw) if raw.exists() else None,
                 'failing_requirement_features':issues,'dynamic_standard_features':{k:features[k] for k in ('FET_004_STANDARD','FET_022_STANDARD','FET_024_STANDARD') if k in features},
                 'optional_unmet_features':[k for k,v in features.items() if not v.get('passed') and k not in required],
                 'scope':'Exact-input static NVIDIA profile only; no implied engine runtime or hardware qualification'}
    except Exception as exc:
        if isinstance(exc,subprocess.TimeoutExpired):
            (output/'stdout.log').write_bytes(exc.stdout or b'');(output/'stderr.log').write_bytes(exc.stderr or b'')
        summary={'status':'failed','input_sha256':h,'profile':profile,'profile_version':version,
                 'error_type':type(exc).__name__,'error':str(exc),'required_features':required}
    write(output/'summary.json',summary);return summary

def audit(root,record,python,profiles,profile_selection='baseline'):
    target=record['targets'][0];package=inside(root,target['package_path']);p=read(package/'package.json');usd=inside(package,p['entrypoints']['usd'])
    profile=record.get('simready_profile','Robotics-Prop');version={'Robotics-Prop':'3.2.0','Robot-Body':'2.1.0','Robot-Gripper':'2.1.0'}[profile]
    filename={'Robotics-Prop':'robotics_prop.toml','Robot-Body':'robot_body.toml','Robot-Gripper':'robot_gripper.toml'}[profile]
    definitions=tomllib.loads((Path(profiles)/filename).read_text(encoding='utf8'))[profile]
    if profile_selection not in ('baseline','latest'):raise ValueError('Unknown profile selection')
    if profile_selection=='latest':version=max(definitions,key=lambda value:tuple(map(int,value.split('.'))))
    definition=definitions[version]
    required=[next(iter(f)) for f in definition['features'] if not f.get('optional',False)]
    folder=(inside(root,record['supplemental']['metadata_path']).parent if record.get('supplemental') else package.parent/'审查/2026-11-04')/('SimReady-latest' if profile_selection=='latest' else 'SimReady');folder.mkdir(parents=True,exist_ok=True)
    # Reports append by design in the vendor CLI; refusal avoids merging independent runs.
    raw=folder/'nvidia-validation.json'
    if raw.exists():raise ValueError('官方验收回执已存在，不能覆盖 '+str(raw))
    executable=Path(python).with_name('simready-validate.exe')
    command=[str(executable),'--profile',profile,'--version',version,'--output',str(raw),str(usd)]
    h=digest(usd);run=subprocess.run(command,capture_output=True,timeout=180)
    (folder/'stdout.log').write_bytes(run.stdout);(folder/'stderr.log').write_bytes(run.stderr)
    assert digest(usd)==h,'官方检查修改了输入'
    result=read(raw) if raw.exists() else {};details=next(iter(result.values()),{});features=details.get('features_summary',{})
    verdict=evaluate_report(details,required,profile,version,run.returncode)
    issues={}
    for key,feature in features.items():
        for requirement in ast.literal_eval(feature.get('failing requirements','[]')):
            issues.setdefault(requirement,[]).append(key)
    summary={'status':'passed' if verdict['official_profile_passed'] else 'failed','scope':'NVIDIA static profile validation only; no engine benchmark or CAD roundtrip qualification','profile':profile,'profile_version':version,'validator_version':details.get('simready_validate_version'),'tier_versions':details.get('tier_versions'),
             'input_sha256':h,'package_digest':target['package_digest'],'exit_code':run.returncode,'required_features':required,**verdict,'total_features_reported':len(features),'passed_features':[k for k,v in features.items() if v.get('passed') is True],
             'failing_requirement_features':issues,'no_asset_stamp':True,'raw_report':raw.relative_to(root).as_posix(),'profile_definition_sha256':digest(Path(profiles)/filename)}
    write(folder/'summary.json',summary)
    internal=inspect(usd,package);write(folder/'internal-usd-check.json',internal)
    record['qualification']['nvidia_simready']={'status':summary['status'],'profile':profile+'@'+version,'runtime':summary['validator_version'],'evidence':(folder/'summary.json').relative_to(root).as_posix()}
    record['qualification']['usd_structure']=internal['status']
    return {'catalog_id':record['catalog_id'],'title':record['title'],'summary':summary,'internal_status':internal['status']}

if __name__=='__main__':
    ap=argparse.ArgumentParser();ap.add_argument('--root',required=True);ap.add_argument('--imports',required=True,nargs='+');ap.add_argument('--python',required=True);ap.add_argument('--profiles',required=True);ap.add_argument('--output',required=True);ap.add_argument('--profile-selection',choices=['baseline','latest'],default='baseline');a=ap.parse_args()
    root=Path(a.root).resolve();index=read(root/'索引/资产索引.json');ids={r['catalog_id'] for f in a.imports for r in read(f)['imported']};reports=[]
    for record in index['entries']:
        if record['catalog_id'] in ids:reports.append(audit(root,record,a.python,a.profiles,a.profile_selection))
    write(root/'索引/资产索引.json',index);write(a.output,{'status':'completed','reports':reports,'official_passed':sum(r['summary']['status']=='passed' for r in reports),'official_failed':sum(r['summary']['status']=='failed' for r in reports)})
    print(json.dumps({'completed':len(reports),'official_passed':sum(r['summary']['status']=='passed' for r in reports)}))
