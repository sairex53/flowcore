"""Experimental forecasts; bounded history, causal scoring and no kernel authority."""
import ctypes
import uuid
from collections import deque
from pathlib import Path
from paths import BINDIR

METHODS=('ema','trend','queue-growth','rate-change','hybrid')
HORIZON=2.0


class Sample(ctypes.Structure):
    _fields_=[('time_ns',ctypes.c_uint64),('backlog_bytes',ctypes.c_double),
              ('departure_bps',ctypes.c_double),('capacity_bps',ctypes.c_double)]


class Forecast(ctypes.Structure):
    _fields_=[(name,ctypes.c_double) for name in ('ema_bytes','trend_bps','arrival_proxy_bps',
              'forecast_bytes','risk','quality','lead_seconds','residual_bytes')]


class Native:
    def __init__(self):
        self.library=ctypes.CDLL(str(Path(BINDIR)/'libflowcore_intelligence.so'))
        self.function=self.library.flowcore_predict_v1
        self.function.argtypes=[ctypes.POINTER(Sample),ctypes.c_uint,ctypes.c_uint,
                               ctypes.c_double,ctypes.c_double,ctypes.c_double,ctypes.POINTER(Forecast)]
        self.function.restype=ctypes.c_int

    def forecast(self,rows,method,threshold,utilization):
        array=(Sample*len(rows))(*(Sample(row['time_ns'],row['backlog_bytes'],
                                  row['departure_bps'],row['capacity_bps']) for row in rows))
        out=Forecast()
        result=self.function(array,len(rows),METHODS.index(method),HORIZON,threshold,utilization,ctypes.byref(out))
        if result<0: raise ValueError('invalid native prediction inputs')
        return None if result else {field:getattr(out,field) for field,_ in out._fields_}


def metrics():
    return dict(tp=0,fp=0,fn=0,tn=0,censored=0,alerts=0,early_alerts=0,early_hits=0,early_evaluated=0,
                lead_sum_seconds=0,lead_count=0,onsets=0,missed_onsets=0,warning_samples=0,bins=[dict(n=0,events=0) for _ in range(5)])


class Predictor:
    def __init__(self):
        self.instance=uuid.uuid4().hex
        self.native=None
        self.history={name:deque(maxlen=64) for name in ('critical','interactive','standard')}
        self.pending=deque(maxlen=128)
        self.outcomes=deque(maxlen=128)
        self.counts={method:metrics() for method in METHODS}
        self.latest={}
        self.previous={}
        self.capacity=None
        self.error=None
        self.samples=0
        self.last_ns=0
        self.elapsed_seconds=0.0

    def invalidate(self,reason):
        for item in self.pending: self.counts[item['method']]['censored']+=1
        self.pending.clear()
        for rows in self.history.values(): rows.clear()
        self.previous.clear();self.latest.clear();self.error=reason

    def observe(self,stamp,tc,capacity,rates,application,rtt,allocation):
        if stamp<=self.last_ns: raise ValueError('non-monotonic predictive observation')
        elapsed=(stamp-self.last_ns)/1e9 if self.last_ns else None
        if elapsed is not None and elapsed<.75: return # verified actuator refresh != new observation
        if self.capacity!=capacity or elapsed is not None and elapsed>2.5:
            self.invalidate('capacity change or telemetry gap')
        self.capacity=capacity
        if elapsed is not None and elapsed<=2.5: self.elapsed_seconds+=elapsed
        self.last_ns=stamp;self.samples+=1
        classes=tc.get('traffic_classes',{})
        leaves={row.get('parent'):row for row in tc.get('qdisc',[]) if row.get('parent')}
        root=next((row for row in tc.get('qdisc',[]) if row.get('root')), {})
        prior=self.previous.get('root')
        utilization=None
        if prior and root.get('bytes',0)>=prior['bytes']:
            utilization=(root['bytes']-prior['bytes'])/(stamp-prior['time_ns'])*1e9/(capacity*125)
        self.previous['root']=dict(time_ns=stamp,bytes=root.get('bytes',0))
        current={}
        for name,rows in self.history.items():
            cls=classes.get(name,{})
            leaf=leaves.get(cls.get('handle'),{})
            previous=self.previous.get(name)
            values=cls.get('stats',{})
            self.previous[name]=dict(time_ns=stamp,bytes=values.get('bytes'),drops=leaf.get('drops'))
            if (not previous or utilization is None or type(leaf.get('backlog')) is not int or
                    type(values.get('bytes')) is not int or type(previous['bytes']) is not int or
                    values['bytes']<previous['bytes']):
                rows.clear();continue
            seconds=(stamp-previous['time_ns'])/1e9
            departures=(values['bytes']-previous['bytes'])/seconds
            drops=((leaf['drops']-previous['drops'])/seconds if type(leaf.get('drops')) is int
                   and type(previous['drops']) is int and leaf['drops']>=previous['drops'] else None)
            threshold=max(1514,rates[name]*125*.02)
            row=dict(time_ns=stamp,backlog_bytes=leaf['backlog'],departure_bps=departures,
                     capacity_bps=capacity*125,configured_capacity_kbit=capacity,
                     drops_per_second=drops,utilization=utilization,application=application,
                     rtt=rtt,allocation=allocation,threshold_bytes=threshold,
                     event=leaf['backlog']>=threshold and utilization>=.85)
            services=[value for value in application.values() if value.get('traffic_class')==name]
            row['risk_context']=dict(queue_serialization_threshold_bytes=threshold,
                capacity_saturation=utilization,
                service_p99_to_target=max((value['p99_us']/value['target_p99_us'] for value in services
                    if value.get('p99_us') is not None),default=None),
                deadline_misses=sum(value.get('deadline_miss',0) for value in services))
            rows.append(row);current[name]=row
        for name,row in current.items():
            history=self.history[name]
            if len(history)>1 and row['event'] and not history[-2]['event']:
                for method,count in self.counts.items():
                    count['onsets']+=1
                    warned=any(item['method']==method and item['service_class']==name and
                               item['early'] and item['issued_ns']<stamp<=item['end_ns'] and
                               item['allocation']==allocation and not item['censored'] for item in self.pending)
                    count['missed_onsets']+=int(not warned)
        self._resolve(stamp,current,allocation)
        previous_forecasts=self.latest
        self.latest={}
        if self.native is None: self.native=Native()
        for name,row in current.items():
            forecasts={}
            for method in METHODS:
                prediction=self.native.forecast(list(self.history[name]),method,row['threshold_bytes'],utilization)
                if prediction is None: continue
                warning=prediction['risk']>=.8 and prediction['quality']>=.45
                early=warning and not row['event'] and 0.1<prediction['lead_seconds']<=HORIZON
                detail=dict(prediction,time_ns=stamp,method=method,horizon_seconds=HORIZON,
                            warning=warning,actionable=early,current_event=row['event'],
                            input=row,calibrated_probability=False,
                            confidence='heuristic_history_quality',samples=len(self.history[name]))
                forecasts[method]=detail
                count=self.counts[method]
                count['warning_samples']+=int(warning)
                count['alerts']+=int(warning and not previous_forecasts.get(name,{}).get(method,{}).get('warning',False))
                count['early_alerts']+=int(early)
                if len(self.pending)==self.pending.maxlen:
                    dropped=self.pending.popleft();self.counts[dropped['method']]['censored']+=1
                self.pending.append(dict(service_class=name,method=method,issued_ns=stamp,
                    end_ns=stamp+round(HORIZON*1e9),warning=warning,early=early,
                    risk=prediction['risk'],event=False,observed=0,first_event_ns=None,allocation=allocation,
                    censored=False))
            if forecasts: self.latest[name]=forecasts
        self.error=None

    def _resolve(self,stamp,current,allocation):
        unresolved=deque(maxlen=128)
        for item in self.pending:
            row=current.get(item['service_class'])
            if row is None or item['allocation']!=allocation: item['censored']=True
            # Only observations strictly after issue and at/before the forecast horizon label it.
            if row and item['issued_ns']<stamp<=item['end_ns']:
                item['observed']+=1
                if row['event']:
                    item['event']=True
                    if item['first_event_ns'] is None: item['first_event_ns']=stamp
            if stamp<item['end_ns']:
                unresolved.append(item);continue
            count=self.counts[item['method']]
            if item['censored'] or not item['observed'] or stamp-item['end_ns']>1500000000:
                count['censored']+=1;item['outcome']='inconclusive'
            else:
                count['early_evaluated']+=int(item['early'])
                result=('tp' if item['event'] else 'fp') if item['warning'] else ('fn' if item['event'] else 'tn')
                count[result]+=1;item['outcome']=result
                bucket=count['bins'][min(4,int(item['risk']*5))]
                bucket['n']+=1;bucket['events']+=int(item['event'])
                if item['early'] and item['event']:
                    lead=(item['first_event_ns']-item['issued_ns'])/1e9
                    count['early_hits']+=1;count['lead_sum_seconds']+=lead;count['lead_count']+=1
            self.outcomes.append(item)
        self.pending=unresolved

    def status(self):
        result={}
        for method,count in self.counts.items():
            result[method]=dict(count,precision=count['tp']/(count['tp']+count['fp']) if count['tp']+count['fp'] else None,
                recall=count['tp']/(count['tp']+count['fn']) if count['tp']+count['fn'] else None,
                alert_frequency_per_minute=count['alerts']*60/self.elapsed_seconds if self.elapsed_seconds else None,
                early_precision=count['early_hits']/count['early_evaluated'] if count['early_evaluated'] else None)
        return dict(instance=self.instance,experimental=True,default='shadow; active requires explicit controller start',
                    error=self.error,state='available' if self.latest else 'insufficient_data',
                    samples=self.samples,latest=self.latest,metrics=result,
                    pending=len(self.pending),history_limit=64,outcome_limit=128,
                    recent_outcomes=list(self.outcomes)[-15:])
