"""验证公共来源装配真正选择读取端，并拒绝来源冲突及降低完成依赖。"""
import pytest

from unilabos.simulation.assembly import Provider, SourceAssembly, SourceCatalog, resolve_completion

IDENTITY=dict(world_id='w',session_id='s',epoch=0)


def provider(source,ref,**kwargs):
    """构造布尔值测试提供者，允许覆盖采样或完成用途声明。"""
    return Provider(source,ref,'1',bool,source,**kwargs)


def test_select_reads_actual_provider_without_rewriting_provenance():
    """验证只调用所选来源，错误来源标签及过期原样本不能通过屏障。"""
    a,b=provider('sensor','a'),provider('inventory','b')
    catalog=SourceCatalog({'present':[a,b]})
    calls=[]
    def read(source,ref,value):
        """创建记录调用来源的采样函数，供选择和伪标来源反例使用。"""
        def sample(context):
            """返回固定第三步的来源帧并记下 ref，不随消费时间刷新采集时间。"""
            calls.append(ref)
            return dict(source=source,source_ref=ref,unit='1',value=value,quality='good',captured_tick=3,sequence=3,**IDENTITY)
        return sample
    readers={'a':read('sensor','a',True),'b':read('inventory','b',False)}
    selected=catalog.select({'present':{'source':'inventory','ref':'b'}},{'present':'sensor'})
    assembly=SourceAssembly(selected,readers,IDENTITY)
    assert assembly.read({},tick=3)=={'present':False} and calls==['b']
    # 提供者谎报来源时拒绝，不根据配置改成 inventory。
    bad=SourceAssembly(selected,{'b':read('sensor','b',False)},IDENTITY)
    with pytest.raises(ValueError,match='sample_invalid'):bad.read({},tick=3)
    with pytest.raises(ValueError,match='sample_invalid'):assembly.read({},tick=6)


def test_catalog_refuses_ambiguous_unknown_conflicting_and_double_writers():
    """验证重复写入者、单位冲突、歧义选择、未知端口和缺失读取端都被拒绝。"""
    a=provider('sensor','a')
    with pytest.raises(ValueError,match='multiple_writers'):
        SourceCatalog({'p':[a,Provider('sensor','a','1',bool,'other')]})
    with pytest.raises(ValueError,match='contract_conflict'):
        SourceCatalog({'p':[a,Provider('sensor','b','mL',float,'other')]})
    catalog=SourceCatalog({'p':[a,provider('sensor','b')]})
    for selected in ({'p':'sensor'},{'p':{'source':'sensor','ref':'missing'}},{'unknown':'sensor'}):
        with pytest.raises(ValueError):catalog.select(selected,{'p':{'source':'sensor','ref':'a'}})
    selected=catalog.select({}, {'p':{'source':'sensor','ref':'a'}})
    with pytest.raises(ValueError,match='not_ready'):SourceAssembly(selected,{},IDENTITY)


def test_completion_cannot_remove_required_ports_or_admit_display_only_sources():
    """验证策略允许增加合法证据，但拒绝删依赖、重复端口及显示专用来源。"""
    ports={'contact':provider('sensor','contact'),'display':provider('sensor','display',completion=False),'extra':provider('inventory','extra')}
    policies={'package.v1':{'contact'}}
    assert resolve_completion({'policy':'package.v1','required_ports':['contact','extra']},policies,ports)==('package.v1',('contact','extra'))
    for choice in ('unknown',{'policy':'package.v1','required_ports':[]},
        {'policy':'package.v1','required_ports':['contact','display']},{'policy':'package.v1','required_ports':['contact','contact']}):
        with pytest.raises(ValueError):resolve_completion(choice,policies,ports)
