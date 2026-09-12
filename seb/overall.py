"""An explicit target-wise aggregate, with missing coverage kept visible."""
import statistics
import math


def macro_spearman(targets, expected_counts, *, minimum_models=3, constant_prediction='zero'):
    components={};excluded={};values=[];complete=True
    for target,expected in expected_counts.items():
        if expected<minimum_models:
            excluded[target]='insufficient_frozen_reference_coverage';continue
        row=targets.get(target,{})
        n=row.get('n_predicted',row.get('n',0));rho=row.get('spearman')
        coverage=n/expected
        status='valid'
        if n!=expected:
            component=None;status='incomplete_coverage';complete=False
        elif rho is None:
            # Undefined per-target rho stays undefined; aggregate conversion is an explicit convention.
            if constant_prediction=='zero' and row.get('constant_prediction',False):
                component=0.;status='constant_prediction_zero_rank_information'
            else:component=None;status='undefined_correlation';complete=False
        elif not isinstance(rho,(int,float)) or isinstance(rho,bool) or not math.isfinite(rho) or not -1.000000000001<=rho<=1.000000000001:
            component=None;status='invalid_correlation';complete=False
        else:component=max(-1.,min(1.,float(rho)))
        components[target]={'n':n,'expected_n':expected,'coverage':coverage,'spearman':rho,'component':component,'status':status}
        if component is not None:values.append(component)
    diagnostic=statistics.mean(values) if values else None
    return {'metric':'macro_spearman','score':diagnostic if complete and values else None,
            'status':'valid' if complete and values else 'incomplete','range':[-1,1],
            'diagnostic_available_target_mean':diagnostic,'components':components,'excluded_targets':excluded,
            'eligible_targets':len(components),'scored_targets':len(values)}
