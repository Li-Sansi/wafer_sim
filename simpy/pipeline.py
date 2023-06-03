import simpy
import math
from typing import List,Optional

from util import *
from tile_dataflow import Tile
from wafer_device import Wafer_Device as wd
from wafer_device import Packet
import ML
#TODO 修改流水线调度策略
# reference:Megatron2.0 
# @fangjh21.20230602



class Stage():
    __stage_id=0
    def __init__(self,tile,env,op_list,last_core_id:Optional[List[int]],\
                 cur_core_id:Optional[List[int]],next_core_id:Optional[List[int]])-> None:
        self.tile=tile
        self.op_list=op_list
        self.i_shape=[]
        self.o_shape=[]
        self.__init_info()

        self.last_core_id=last_core_id
        self.cur_core_id=cur_core_id
        self.next_core_id=next_core_id
        self.stage_info=[]
        self.map_ana=[]
        #simpy env 
        self.worker= simpy.Resource(env, capacity=1)
        self.trace=[]
        
        self.__class__.__stage_id+=1
    def __init_info(self):
        self.i_shape=self.op_list[0].i_shape
        self.o_shape=self.op_list[-1].o_shape
        
    def stage_forward_process(self,last_q:simpy.Store,next_q:simpy.Store,env:simpy.Environment,noc:wd):
        def pro():
            with self.worker.request() as req:
                yield req
                pks = yield last_q.get()
                #print('i_shape',self.i_shape)
                #print('pks_shape',pks.shape)
                assert(self.i_shape==pks.shape)
                t_last=env.now
                #print('time:{},start forward'.format(round(env.now, 2)))
                #TODO 修改为数据流执行
                #yield env.timeout(20)
                yield env.process(Tile.execute_forward_process(self.tile,env,self.map_ana,self.cur_core_id,self.op_list,noc))
                self.trace.append((t_last,env.now,0))
            if self.next_core_id!=None and self.next_core_id!=[]:
                task_info=self.__class__.__stage_id
                yield env.process(noc.STAGE_PASS_process(pks,self.cur_core_id,self.next_core_id,task_info))
            else:
                pass
        while True:
                yield env.process(pro())
                yield next_q.put(Packet('',self.o_shape))
                break
    def stage_backward_process(self,last_q:simpy.Store,next_q:simpy.Store,env:simpy.Environment,noc:wd):
        def pro():
            with self.worker.request() as req:
                yield req
                pks = yield next_q.get()
                #print('o_shape',self.o_shape)
                #print('pks_shape',pks.shape)
                assert(self.o_shape==pks.shape)
                t_last=env.now
                #TODO 修改为数据流执行
                yield env.process(Tile.execute_backward_process(self.tile,env,self.map_ana,self.cur_core_id,self.op_list,noc))
                self.trace.append((t_last,env.now,1))
            if self.last_core_id!=None and self.last_core_id!=[]:
                task_info=self.__class__.__stage_id
                yield env.process(noc.STAGE_PASS_process(pks,self.cur_core_id,self.last_core_id,task_info))
            else:
                pass
        while True:
                yield env.process(pro())
                yield last_q.put(Packet('',self.i_shape))
                break
class Stages():
    def __init__(self,env,mini_batch_size,micro_batch_size,stages:List[Stage],noc:wd,pipe_type=ML.pipe_strategy.Megatron1F1B) -> None:
        self.env=env
        self.stages=stages
        self.pipe_type=pipe_type
        self.noc=noc
        self.loss_q=simpy.Store(env=self.env,capacity=1) 
        self.mini_batch=mini_batch_size
        self.micro_batch=micro_batch_size
        self.pipe_times=math.ceil(self.mini_batch/self.micro_batch)
        self.f_q=[]
        self.b_q=[]
        self.__set_stage()
    def __set_stage(self):
        #TODO 需要检查device 在stage段无重复，否则映射不符合流水规则
        stages_len=len(self.stages)
        for i in range(stages_len):
            if self.pipe_type==ML.pipe_strategy.GPipe:
                self.stages[i].stage_info=[self.pipe_type,self.mini_batch,self.micro_batch]
            elif self.pipe_type==ML.pipe_strategy.Megatron1F1B:
                self.stages[i].stage_info=[self.pipe_type,i+1,stages_len]
            elif self.pipe_type==ML.pipe_strategy.Cerebras:
                self.stages[i].stage_info=[self.pipe_type,i+1,stages_len]
            else:
                raise NotImplementedError
            self.stages[i].map_ana=Tile.mapping_analysis(self.stages[i].tile,self.stages[i].stage_info,\
                                                         self.stages[i].cur_core_id,self.stages[i].op_list,self.noc)
    def pipeline_execute_forward_process(self):
        def pro():
            stage_len=len(self.stages)
            for i in range(stage_len):
                yield self.env.process(self.stages[i].stage_forward_process(self.f_q[i],self.f_q[i+1],self.env,self.noc))
            #print('finish forward @ {:.3f} us'.format(self.env.now))
        yield self.env.process(pro())
        
    def pipeline_execute_backward_process(self): 
        def pro():
            stage_len=len(self.stages)
            for i in range(stage_len-1,-1,-1):
                yield self.env.process(self.stages[i].stage_backward_process(self.b_q[i],self.b_q[i+1],self.env,self.noc))
            #TODO slove bug  
            #print('finish backward @ {:.3f} us'.format(self.env.now))
        with self.f_q[len(self.stages)].get() as get:
            a=yield get
            yield self.b_q[len(self.stages)].put(a)
            yield self.env.process(pro())
            
    def start(self):
        for i in range(self.pipe_times):
            task_info='input_data_fetch_'+str(i)
            i_shape=self.stages[0].i_shape
            with self.f_q[0].put(Packet(task_info,i_shape)) as put:
                yield put
                yield self.env.process(self.noc.dram_read_group_process(i_shape,self.stages[0].cur_core_id,task_id=task_info))

    def pipeline(self):
        for _ in range(len(self.stages)+1):
            self.f_q.append(simpy.Store(self.env,capacity=1))
            self.b_q.append(simpy.Store(self.env,capacity=1))
        self.env.process(self.start())
        for i in range(self.pipe_times):
            self.env.process(self.pipeline_execute_forward_process())
            self.env.process(self.pipeline_execute_backward_process())
            #self.env.timeout(1e-11)
    def pipe_status(self,path,draw_pipe=True):
        all_trace=[]
        name=str(self.pipe_type)
        for stage in self.stages:
            all_trace.append(stage.trace)
        if draw_pipe:
            draw_pipeline(all_trace,path=path,title=name)
        pipe_endtime=all_trace[0][-1][1]
        print('ml {} training pipeline endtime:{:.3f} us'.format(name,pipe_endtime))
        return pipe_endtime

