"""

The primary SrNet class

"""


# -- torch network deps --
import torch as th
import torch.nn as nn
from einops import rearrange,repeat
from functools import partial

# -- extra deps --
from timm.models.layers import trunc_normal_

# -- project deps --
from .basic import BasicBlockList
from .scaling import Downsample,Upsample
from .proj import InputProj,InputProjSeq,OutputProj,OutputProjSeq
from ..utils.model_utils import apply_freeze

# -- benchmarking --
from ..utils.timer import ExpTimerList

# -- clean coding --
# from . import attn_mods
# from dev_basics.utils import clean_code

# @clean_code.add_methods_from(bench_mods)
class SrNet(nn.Module):

    def __init__(self, arch_cfg, block_cfg, attn_cfg,
                 search_cfg, normz_cfg, agg_cfg, up_cfg, down_cfg):
        super().__init__()

        # -- init --
        self.num_blocks = len(block_cfg)
        assert self.num_blocks % 2 == 1,"Must be odd."
        self.num_encs = len(block_cfg)//2
        self.num_decs = len(block_cfg)//2
        self.dd_in = arch_cfg.dd_in
        num_encs = self.num_encs
        self.pos_drop = nn.Dropout(p=arch_cfg.drop_rate_pos)

        # -- dev --
        self.inspect_print = False

        # -- benchmarking --
        self.attn_timer = arch_cfg.attn_timer
        self.times = ExpTimerList(arch_cfg.attn_timer)

        # -- input/output --
        self.input_proj = InputProjSeq(depth=arch_cfg.input_proj_depth,
                                       in_channel=arch_cfg.dd_in,
                                       out_channel=arch_cfg.embed_dim,
                                       kernel_size=3, stride=1, act_layer=nn.LeakyReLU)
        self.output_proj = OutputProj(in_channel=2*arch_cfg.embed_dim,
                                      out_channel=arch_cfg.in_chans,
                                      kernel_size=3,stride=1)

        # -- encoder layers --
        enc_list = []
        for l_enc in range(num_encs):

            # -- init --
            block_cfg_l = block_cfg[l_enc]
            attn_cfg_l = attn_cfg[l_enc]
            search_cfg_l = search_cfg[l_enc]
            normz_cfg_l = normz_cfg[l_enc]
            agg_cfg_l = agg_cfg[l_enc]
            down_cfg_l = down_cfg[l_enc]
            block_cfg_l.type = "enc"
            attn_cfg_l.type = "enc"
            enc_layer = BasicBlockList(block_cfg_l,attn_cfg_l,search_cfg_l,
                                       normz_cfg_l,agg_cfg_l)
            down_layer = Downsample(down_cfg_l.in_dim,down_cfg_l.out_dim)
            setattr(self,"encoderlayer_%d" % l_enc,enc_layer)
            setattr(self,"dowsample_%d" % l_enc,down_layer)

            # -- add to list --
            paired_layer = [enc_layer,down_layer]
            enc_list.append(paired_layer)
        self.enc_list = enc_list

        # -- center --
        block_cfg_l = block_cfg[num_encs]
        block_cfg_l.type = "conv"
        attn_cfg_l = attn_cfg[num_encs]
        attn_cfg_l.type = "conv"
        search_cfg_l = search_cfg[num_encs]
        normz_cfg_l = normz_cfg[num_encs]
        agg_cfg_l = agg_cfg[num_encs]
        setattr(self,"conv",BasicBlockList(block_cfg_l,attn_cfg_l,search_cfg_l,
                                           normz_cfg_l,agg_cfg_l))

        # -- decoder --
        dec_list = []
        for l_dec in range(num_encs+1,2*num_encs+1):

            # -- init --
            block_cfg_l = block_cfg[l_dec]
            attn_cfg_l = attn_cfg[l_dec]
            search_cfg_l = search_cfg[l_dec]
            normz_cfg_l = normz_cfg[l_dec]
            agg_cfg_l = agg_cfg[l_dec]
            up_cfg_l = up_cfg[l_dec-(num_encs+1)]
            # up_cfg_l = up_cfg[l_dec]
            block_cfg_l.type = "dec"
            attn_cfg_l.type = "dec"
            up_layer = Upsample(up_cfg_l.in_dim,up_cfg_l.out_dim)
            dec_layer = BasicBlockList(block_cfg_l,attn_cfg_l,search_cfg_l,
                                       normz_cfg_l,agg_cfg_l)
            setattr(self,"upsample_%d" % l_dec,up_layer)
            setattr(self,"decoderlayer_%d" % l_dec,dec_layer)

            # -- add to list --
            paired_layer = [up_layer,dec_layer]
            dec_list.append(paired_layer)

        self.dec_list = dec_list
        self.apply(self._init_weights)

    def _apply_freeze(self):
        if all([f is False for f in self.freeze]): return
        apply_freeze(self,self.freeze)

    def _init_weights(self, m):
        if isinstance(m, nn.Linear):
            trunc_normal_(m.weight, std=.02)
            if isinstance(m, nn.Linear) and m.bias is not None:
                nn.init.constant_(m.bias, 0)
        elif isinstance(m, nn.LayerNorm):
            nn.init.constant_(m.bias, 0)
            nn.init.constant_(m.weight, 1.0)

    @th.jit.ignore
    def no_weight_decay(self):
        return {'absolute_pos_embed'}

    @th.jit.ignore
    def no_weight_decay_keywords(self):
        return {'relative_position_bias_table'}

    def forward(self, vid, flows=None, states=None):

        # -- Input Projection --
        b,t,c,h,w = vid.shape
        y = self.input_proj(vid)
        y = self.pos_drop(y)
        num_encs = self.num_encs

        # -- init states --
        if states is None:
            states = [None for _ in range(2*num_encs+1)]

        # -- enc --
        z = y
        encs = []
        for i,(enc,down) in enumerate(self.enc_list):
            # -- create state --
            states_i = [states[i],states[i+1]]

            # -- forward --
            iH,iW = z.shape[-2:]
            z = enc(z,flows=flows,state=states_i)
            self.iprint("[enc] i: %d" % i,z.shape)
            encs.append(z)
            z = down(z)
            self.iprint("[dow] i: %d" % i,z.shape)

        # -- middle --
        iH,iW = z.shape[-2:]
        z = self.conv(z,flows=flows)
        self.iprint("[mid]: ",z.shape)

        # -- dec --
        for i,(up,dec) in enumerate(self.dec_list):

            # -- create state --
            sinds = [i-1+num_encs,i+num_encs,i]
            states_i = [states[j] for j in sinds]

            # -- forward --
            i_rev = (num_encs-1)-i
            iH,iW = z.shape[-2:]
            z = up(z)
            self.iprint("[up] i: %d" % i,z.shape)
            z = th.cat([z,encs[i_rev]],-3)
            self.iprint("[cat] i: %d" % i,z.shape)
            z = dec(z,flows=flows,state=states_i)
            self.iprint("[dec] i: %d" % i,z.shape)

        # -- Output Projection --
        y = self.output_proj(z)
        self.iprint("y.shape: ",y.shape)

        # -- residual connection --
        out = vid + y if self.dd_in == 3 else y

        # -- timing --
        self.update_block_times()

        return out

    @property
    def max_batch_size(self):
        return -1

    def iprint(self,*args,**kwargs):
        if self.inspect_print:
            print(*args,**kwargs)

    def reset_times(self):
        if self.attn_timer is False: return
        self.times = ExpTimerList(self.use_timer)
        for i in range(self.num_encs):
            layer_i = getattr(self,"encoderlayer_%d" % i)
            layer_i.reset_times()
            layer_i = getattr(self,"decoderlayer_%d" % i)
            layer_i.reset_times()
        layer_i = getattr(self,"conv")
        layer_i.reset_times()

    def update_block_times(self):
        if self.attn_timer is False: return
        for i in range(self.num_encs):
            layer_i = getattr(self,"encoderlayer_%d" % i)
            self.times.update_times(layer_i.times)
            layer_i.reset_times()
            layer_i = getattr(self,"decoderlayer_%d" % i)
            self.times.update_times(layer_i.times)
            layer_i.reset_times()
        layer_i = getattr(self,"conv")
        self.times.update_times(layer_i.times)
        layer_i.reset_times()

    def flops(self,h,w):

        # -- init flops --
        flops = 0

        # -- Input Projection --
        flops += self.input_proj.flops(h,w)
        num_encs = self.num_enc_layers

        # -- enc --
        encs = []
        for i,(enc,down) in enumerate(self.enc_list):
            _h,_w = h//(2**i),w//(2**i)
            flops += enc.flops(_h,_w)
            flops += down.flops(_h,_w)

        # -- middle --
        mod = 2**num_encs
        _h,_w = h//mod,w//mod
        flops += self.conv.flops(_h,_w)

        # -- dec --
        for i,(up,dec) in enumerate(self.dec_list):
            i_rev = num_encs-1-i
            _h,_w = h//(2**(i_rev)),w//(2**(i_rev))
            flops += up.flops(_h,_w)
            flops += dec.flops(_h,_w)

        # -- Output Projection --
        flops += self.output_proj.flops(h,w)

        return flops

