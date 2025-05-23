# Copyright 2024-2025 The Alibaba Wan Team Authors. All rights reserved.
import math

import torch
import torch.profiler as torch_profiler
import torch.cuda.amp as amp
import torch.nn as nn
from diffusers.configuration_utils import ConfigMixin, register_to_config
from diffusers.models.modeling_utils import ModelMixin
import triton
import triton.language as tl


from .attention import flash_attention


__all__ = ['WanModel']

@triton.jit
def rope_kernel(
    x_ptr,
    pos_ptr,
    freqs_ptr,
    n_heads,
    HEAD_DIM: tl.constexpr,
    HEAD_DIM2: tl.constexpr,
    HEADS_PER_ELEM: tl.constexpr,
    HEAD_GROUP_SIZE: tl.constexpr,
    HEADS_PER_BLOCK: tl.constexpr,
    APPROX_TRIGO: tl.constexpr,
):
    """
    Apply RoPE to the ``x_ptr`` tensor. The frequencies
    to use per element are provided in the ``freqs_ptr``
    tensor, this way the kernel does not have to know
    about the various scaling schemes and RoPE params.
    The number of heads total in ``x_ptr`` is given by
    ``n_heads``.

    The RoPE computations are performed in float32.

    Constant arguments:
      - ``HEADS_PER_ELEM``: each input element in the
        ``x_ptr`` tensor is composed of this many heads;
      - ``HEAD_DIM``: the dimension of a head, and also
        double the length of the ``freqs_ptr`` tensor
        (must be even);
      - ``HEAD_DIM2``: ``next_power_of_2(HEAD_DIM) // 2``;
      - ``HEAD_GROUP_SIZE``: loop iteration size in heads;
      - ``HEADS_PER_BLOCK``: each kernel block will process
        this many heads;
      - ``APPROX_TRIGO``: whether to use fast approximate
        sin/cos functions (only safe for inputs in
        [-100pi, +100pi]).
    """
    block_idx = tl.program_id(0).to(tl.int64)
    freqs_ofs = tl.arange(0, HEAD_DIM2)
    head_mask = 2 * freqs_ofs < HEAD_DIM
    freqs = tl.load(freqs_ptr + freqs_ofs, mask=head_mask)

    # iterate on head groups of size HEAD_GROUP_SIZE
    for head_idx in range(
        block_idx * HEADS_PER_BLOCK,
        (block_idx + 1) * HEADS_PER_BLOCK,
        HEAD_GROUP_SIZE,
    ):
        grp_ofs = head_idx + tl.arange(0, HEAD_GROUP_SIZE)
        head_pos = tl.load(pos_ptr + grp_ofs // HEADS_PER_ELEM)
        angles = head_pos[:, None] * freqs[None, :]
        tl.static_assert(angles.dtype == tl.float32)

        if APPROX_TRIGO:
            sines, cosines = tl.inline_asm_elementwise(
                asm="""
                sin.approx.f32  $0, $2;
                cos.approx.f32  $1, $2;
                """,
                constraints="=r,=r,r",
                args=[angles],
                dtype=(tl.float32, tl.float32),
                is_pure=True,
                pack=1,
            )
        else:
            sines = tl.sin(angles)
            cosines = tl.cos(angles)

        re_ofs = grp_ofs[:, None] * HEAD_DIM + 2 * freqs_ofs[None, :]
        im_ofs = re_ofs + 1

        mask = (grp_ofs < n_heads)[:, None] & head_mask[None, :]

        re_x = tl.load(x_ptr + re_ofs, mask=mask).to(tl.float32)
        im_x = tl.load(x_ptr + im_ofs, mask=mask).to(tl.float32)

        re_out = re_x * cosines - im_x * sines
        im_out = im_x * cosines + re_x * sines

        tl.store(x_ptr + re_ofs, re_out, mask=mask)
        tl.store(x_ptr + im_ofs, im_out, mask=mask)


def rope_apply_jit(
    x: torch.Tensor,
    pos: torch.Tensor,
    freqs: torch.Tensor,
    approx_trigo: bool = False,
) -> None:
    """
    Apply RoPE in place to the argument tensor.

    Args:
        x (torch.Tensor):
            the tensor to apply RoPE to, with shape
            (N, H, D) where H is the number of heads
            per element and D is the dimension of
            the individual heads modified by RoPE.
        pos (torch.Tensor):
            an integer tensor of shape (N,) giving
            the sequence position of each element
            in ``x``.
        freqs (torch.Tensor):
            the frequencies to use in the RoPE
            computation; this must be a float
            tensor of shape (D/2,).
    """
    if x.ndim != 3:
        raise ValueError(
            "x must be a 3-D tensor; got: {x.ndim=}",
        )
    if not x.is_contiguous():
        raise ValueError("x must be contiguous")

    N, H, D = x.shape

    if pos.ndim != 1 or pos.shape[0] != N:
        raise ValueError(
            f"pos must be a 1-D tensor with size {N}; "
            f"got this instead : {pos.shape=}",
        )
    if not pos.is_contiguous():
        raise ValueError("pos must be contiguous")

    if freqs.ndim != 1 or freqs.shape[0] * 2 != D:
        raise ValueError(
            "freqs must be a 1-D tensor with size {D/2}; "
            f"got this instead: {freqs.shape=}"
        )
    if not freqs.is_contiguous():
        raise ValueError("freqs must be contiguous")

    n_heads = N * H
    if n_heads < 2048:
        HEADS_PER_BLOCK = 1
        HEAD_GROUP_SIZE = 1
    else:
        head_size = D * x.element_size()
        HEADS_PER_BLOCK = max(1, 2048 // head_size)
        HEAD_GROUP_SIZE = triton.next_power_of_2(512 // head_size)

    n_blocks = triton.cdiv(n_heads, HEADS_PER_BLOCK)
    rope_kernel[(n_blocks,)](
        x,
        pos,
        freqs,
        n_heads,
        D,
        triton.next_power_of_2(D) // 2,
        H,
        HEAD_GROUP_SIZE,
        HEADS_PER_BLOCK,
        approx_trigo,
    )


def sinusoidal_embedding_1d(dim, position):
    # preprocess
    assert dim % 2 == 0
    half = dim // 2
    position = position.type(torch.float64)

    # calculation
    sinusoid = torch.outer(
        position, torch.pow(10000, -torch.arange(half).to(position).div(half)))
    x = torch.cat([torch.cos(sinusoid), torch.sin(sinusoid)], dim=1)
    return x


@amp.autocast(enabled=False)
def rope_params(max_seq_len, dim, theta=10000):
    assert dim % 2 == 0
    freqs = torch.outer(
        torch.arange(max_seq_len),
        1.0 / torch.pow(theta, torch.arange(0, dim, 2).div(dim)) # 1e4 ^ (2t/dim)
    )
    freqs = torch.stack(
        [
            torch.cos(freqs), # max_seq_len x dim/2
            -torch.sin(freqs),
            torch.sin(freqs),
            torch.cos(freqs)
        ],
        dim=2
    ) # max_seq_len x dim/2 x 4
    freqs = freqs.view(max_seq_len, dim // 2, 2, 2)
    return freqs


@amp.autocast(enabled=False)
def rope_apply(x : torch.Tensor, grid_sizes, freqs : torch.Tensor):
    # freqs shape [1024, C, 2, 2] where C is the embedding dimension
    # x shape [B, L, num_heads, 2*C] where B is the batch size, L is the sequence length, and C is the embedding dimension
    l, n, c = x.size(1), x.size(2), x.size(3) // 2

    max_seq_len = freqs.size(0)

    # split freqs
    freqs = freqs.split([c - 2 * (c // 3), c // 3, c // 3], dim=1)

    # loop over samples
    output = []
    for i, (f, h, w) in enumerate(grid_sizes):
        seq_len = f * h * w

        # precompute multipliers
        torch._check(seq_len == x.size(1))
        torch._check((f * h * w) == x.size(1))
        x_b = x[i, :seq_len] # seq_len x n x 2c
        x_i = x_b.reshape(*x_b.size()[:-1], -1, 1, 2) # seq_len x n x c x 1 x 2

        torch._check_is_size(f)
        torch._check_is_size(h)
        torch._check_is_size(w)
        torch._check(f <= max_seq_len)
        torch._check(h <= max_seq_len)
        torch._check(w <= max_seq_len)
        freqs_i = torch.cat([
            freqs[0][:f].view(f, 1, 1, -1).expand(f, h, w, -1), # [f, c//3, 2, 2] -> [f, h, w, 4*c//3]
            freqs[1][:h].view(1, h, 1, -1).expand(f, h, w, -1),
            freqs[2][:w].view(1, 1, w, -1).expand(f, h, w, -1)
        ], dim=-1) # [f, h, w, 4*c]
        freqs_i = freqs_i.reshape(-1, 1, c, 2, 2) # [f, h, w, 4*c] -> [seq_len, 1, c, 2, 2]

        # apply rotary embedding
        x_i = (x_i * freqs_i).sum(4).flatten(2) # [seq_len, n, c, 2, 2] -> [seq_len, n, c*2]
        # torch._check_is_size(l - f * h * w, f"{f}, {h}, {w}, {l}, {n}, {c}")
        # torch._check((2 * l - f * h * w) == 0, f"{f}, {h}, {w}, {l}, {n}, {c}")
        x_i = torch.cat([x_i, x[i, seq_len:]]) # [2xseq_len, n, c*2]

        # append to collection
        # NOTE this will only work with one sample in the batch)
        torch._check(2 * l - f * h * w != 1)
        output.append(x_i)
    
    # concatenate all samples
    return torch.stack(output, dim=0) # [B, L, n, c*2]


class WanRMSNorm(nn.Module):

    def __init__(self, dim, eps=1e-5):
        super().__init__()
        self.dim = dim
        self.eps = eps
        self.weight = nn.Parameter(torch.ones(dim))

    def forward(self, x):
        r"""
        Args:
            x(Tensor): Shape [B, L, C]
        """
        return self._norm(x.float()).type_as(x) * self.weight

    def _norm(self, x):
        return x * torch.rsqrt(x.pow(2).mean(dim=-1, keepdim=True) + self.eps)


class WanLayerNorm(nn.LayerNorm):

    def __init__(self, dim, eps=1e-6, elementwise_affine=False):
        super().__init__(dim, elementwise_affine=elementwise_affine, eps=eps)

    def forward(self, x):
        r"""
        Args:
            x(Tensor): Shape [B, L, C]
        """
        return super().forward(x.float()).type_as(x)


class WanSelfAttention(nn.Module):

    def __init__(self,
                 dim,
                 num_heads,
                 window_size=(-1, -1),
                 qk_norm=True,
                 eps=1e-6):
        assert dim % num_heads == 0
        super().__init__()
        self.dim = dim
        self.num_heads = num_heads
        self.head_dim = dim // num_heads
        self.window_size = window_size
        self.qk_norm = qk_norm
        self.eps = eps

        # layers
        self.q = nn.Linear(dim, dim)
        self.k = nn.Linear(dim, dim)
        self.v = nn.Linear(dim, dim)
        self.o = nn.Linear(dim, dim)
        self.norm_q = WanRMSNorm(dim, eps=eps) if qk_norm else nn.Identity()
        self.norm_k = WanRMSNorm(dim, eps=eps) if qk_norm else nn.Identity()

    def forward(self, x, seq_lens, grid_sizes, freqs):
        r"""
        Args:
            x(Tensor): Shape [B, L, num_heads, C / num_heads]
            seq_lens(Tensor): Shape [B]
            grid_sizes(Tensor): Shape [B, 3], the second dimension contains (F, H, W)
            freqs(Tensor): Rope freqs, shape [1024, C / num_heads / 2]
        """
        b, s, n, d = *x.shape[:2], self.num_heads, self.head_dim

        # query, key, value function
        def qkv_fn(x):
            q = self.norm_q(self.q(x)).view(b, s, n, d)
            k = self.norm_k(self.k(x)).view(b, s, n, d)
            v = self.v(x).view(b, s, n, d)
            return q, k, v

        q, k, v = qkv_fn(x)

        x = flash_attention(
            q=rope_apply(q, grid_sizes, freqs),
            k=rope_apply(k, grid_sizes, freqs),
            v=v,
            k_lens=seq_lens,
            window_size=self.window_size)

        # output
        x = x.flatten(2)
        x = self.o(x)
        return x


class WanT2VCrossAttention(WanSelfAttention):

    def forward(self, x, context, context_lens):
        r"""
        Args:
            x(Tensor): Shape [B, L1, C]
            context(Tensor): Shape [B, L2, C]
            context_lens(Tensor): Shape [B]
        """
        b, n, d = x.size(0), self.num_heads, self.head_dim

        # compute query, key, value
        q = self.norm_q(self.q(x)).view(b, -1, n, d)
        k = self.norm_k(self.k(context)).view(b, -1, n, d)
        v = self.v(context).view(b, -1, n, d)

        # compute attention
        x = flash_attention(q, k, v, k_lens=context_lens)

        # output
        x = x.flatten(2)
        x = self.o(x)
        return x


class WanI2VCrossAttention(WanSelfAttention):

    def __init__(self,
                 dim,
                 num_heads,
                 window_size=(-1, -1),
                 qk_norm=True,
                 eps=1e-6):
        super().__init__(dim, num_heads, window_size, qk_norm, eps)

        self.k_img = nn.Linear(dim, dim)
        self.v_img = nn.Linear(dim, dim)
        # self.alpha = nn.Parameter(torch.zeros((1, )))
        self.norm_k_img = WanRMSNorm(dim, eps=eps) if qk_norm else nn.Identity()

    def forward(self, x, context, context_lens):
        r"""
        Args:
            x(Tensor): Shape [B, L1, C]
            context(Tensor): Shape [B, L2, C]
            context_lens(Tensor): Shape [B]
        """
        context_img = context[:, :257]
        context = context[:, 257:]
        b, n, d = x.size(0), self.num_heads, self.head_dim

        # compute query, key, value
        q = self.norm_q(self.q(x)).view(b, -1, n, d)
        k = self.norm_k(self.k(context)).view(b, -1, n, d)
        v = self.v(context).view(b, -1, n, d)
        k_img = self.norm_k_img(self.k_img(context_img)).view(b, -1, n, d)
        v_img = self.v_img(context_img).view(b, -1, n, d)
        img_x = flash_attention(q, k_img, v_img, k_lens=None)
        # compute attention
        x = flash_attention(q, k, v, k_lens=context_lens)

        # output
        x = x.flatten(2)
        img_x = img_x.flatten(2)
        x = x + img_x
        x = self.o(x)
        return x


WAN_CROSSATTENTION_CLASSES = {
    't2v_cross_attn': WanT2VCrossAttention,
    'i2v_cross_attn': WanI2VCrossAttention,
}


class WanAttentionBlock(nn.Module):

    def __init__(self,
                 cross_attn_type,
                 dim,
                 ffn_dim,
                 num_heads,
                 window_size=(-1, -1),
                 qk_norm=True,
                 cross_attn_norm=False,
                 eps=1e-6):
        super().__init__()
        self.dim = dim
        self.ffn_dim = ffn_dim
        self.num_heads = num_heads
        self.window_size = window_size
        self.qk_norm = qk_norm
        self.cross_attn_norm = cross_attn_norm
        self.eps = eps

        # layers
        self.norm1 = WanLayerNorm(dim, eps)
        self.self_attn = WanSelfAttention(dim, num_heads, window_size, qk_norm,
                                          eps)
        self.norm3 = WanLayerNorm(
            dim, eps,
            elementwise_affine=True) if cross_attn_norm else nn.Identity()
        self.cross_attn = WAN_CROSSATTENTION_CLASSES[cross_attn_type](dim,
                                                                      num_heads,
                                                                      (-1, -1),
                                                                      qk_norm,
                                                                      eps)
        self.norm2 = WanLayerNorm(dim, eps)
        self.ffn = nn.Sequential(
            nn.Linear(dim, ffn_dim), nn.GELU(approximate='tanh'),
            nn.Linear(ffn_dim, dim))

        # modulation
        self.modulation = nn.Parameter(torch.randn(1, 6, dim) / dim**0.5)

    def forward(
        self,
        x,
        e,
        seq_lens,
        grid_sizes,
        freqs,
        context,
        context_lens,
    ):
        r"""
        Args:
            x(Tensor): Shape [B, L, C]
            e(Tensor): Shape [B, 6, C]
            seq_lens(Tensor): Shape [B], length of each sequence in batch
            grid_sizes(Tensor): Shape [B, 3], the second dimension contains (F, H, W)
            freqs(Tensor): Rope freqs, shape [1024, C / num_heads / 2]
        """
        assert e.dtype == torch.float32
        with amp.autocast(dtype=torch.float32):
            e = (self.modulation + e).chunk(6, dim=1)
        assert e[0].dtype == torch.float32

        # self-attention
        y = self.self_attn(
            self.norm1(x).float() * (1 + e[1]) + e[0], seq_lens, grid_sizes,
            freqs)
        with amp.autocast(dtype=torch.float32):
            x = x + y * e[2]

        # cross-attention & ffn function
        def cross_attn_ffn(x, context, context_lens, e):
            x = x + self.cross_attn(self.norm3(x), context, context_lens)
            y = self.ffn(self.norm2(x).float() * (1 + e[4]) + e[3])
            with amp.autocast(dtype=torch.float32):
                x = x + y * e[5]
            return x

        x = cross_attn_ffn(x, context, context_lens, e)
        return x


class Head(nn.Module):

    def __init__(self, dim, out_dim, patch_size, eps=1e-6):
        super().__init__()
        self.dim = dim
        self.out_dim = out_dim
        self.patch_size = patch_size
        self.eps = eps

        # layers
        out_dim = math.prod(patch_size) * out_dim
        self.norm = WanLayerNorm(dim, eps)
        self.head = nn.Linear(dim, out_dim)

        # modulation
        self.modulation = nn.Parameter(torch.randn(1, 2, dim) / dim**0.5)

    def forward(self, x, e):
        r"""
        Args:
            x(Tensor): Shape [B, L1, C]
            e(Tensor): Shape [B, C]
        """
        assert e.dtype == torch.float32
        with amp.autocast(dtype=torch.float32):
            e = (self.modulation + e.unsqueeze(1)).chunk(2, dim=1)
            x = (self.head(self.norm(x) * (1 + e[1]) + e[0]))
        return x


class MLPProj(torch.nn.Module):

    def __init__(self, in_dim, out_dim):
        super().__init__()

        self.proj = torch.nn.Sequential(
            torch.nn.LayerNorm(in_dim), torch.nn.Linear(in_dim, in_dim),
            torch.nn.GELU(), torch.nn.Linear(in_dim, out_dim),
            torch.nn.LayerNorm(out_dim))

    def forward(self, image_embeds):
        clip_extra_context_tokens = self.proj(image_embeds)
        return clip_extra_context_tokens


class WanModel(ModelMixin, ConfigMixin):
    r"""
    Wan diffusion backbone supporting both text-to-video and image-to-video.
    """

    ignore_for_config = [
        'patch_size', 'cross_attn_norm', 'qk_norm', 'text_dim', 'window_size'
    ]
    _no_split_modules = ['WanAttentionBlock']

    @register_to_config
    def __init__(self,
                 model_type='t2v',
                 patch_size=(1, 2, 2),
                 text_len=512,
                 in_dim=16,
                 dim=5120,
                 ffn_dim=13824,
                 freq_dim=256,
                 text_dim=4096,
                 out_dim=16,
                 num_heads=40,
                 num_layers=40,
                 window_size=(-1, -1),
                 qk_norm=True,
                 cross_attn_norm=True,
                 eps=1e-6):
        r"""
        Initialize the diffusion model backbone.

        Args:
            model_type (`str`, *optional*, defaults to 't2v'):
                Model variant - 't2v' (text-to-video) or 'i2v' (image-to-video)
            patch_size (`tuple`, *optional*, defaults to (1, 2, 2)):
                3D patch dimensions for video embedding (t_patch, h_patch, w_patch)
            text_len (`int`, *optional*, defaults to 512):
                Fixed length for text embeddings
            in_dim (`int`, *optional*, defaults to 16):
                Input video channels (C_in)
            dim (`int`, *optional*, defaults to 2048):
                Hidden dimension of the transformer
            ffn_dim (`int`, *optional*, defaults to 8192):
                Intermediate dimension in feed-forward network
            freq_dim (`int`, *optional*, defaults to 256):
                Dimension for sinusoidal time embeddings
            text_dim (`int`, *optional*, defaults to 4096):
                Input dimension for text embeddings
            out_dim (`int`, *optional*, defaults to 16):
                Output video channels (C_out)
            num_heads (`int`, *optional*, defaults to 16):
                Number of attention heads
            num_layers (`int`, *optional*, defaults to 32):
                Number of transformer blocks
            window_size (`tuple`, *optional*, defaults to (-1, -1)):
                Window size for local attention (-1 indicates global attention)
            qk_norm (`bool`, *optional*, defaults to True):
                Enable query/key normalization
            cross_attn_norm (`bool`, *optional*, defaults to False):
                Enable cross-attention normalization
            eps (`float`, *optional*, defaults to 1e-6):
                Epsilon value for normalization layers
        """

        super().__init__()

        assert model_type in ['t2v', 'i2v']
        self.model_type = model_type

        self.patch_size = patch_size
        self.text_len = text_len
        self.in_dim = in_dim
        self.dim = dim
        self.ffn_dim = ffn_dim
        self.freq_dim = freq_dim
        self.text_dim = text_dim
        self.out_dim = out_dim
        self.num_heads = num_heads
        self.num_layers = num_layers
        self.window_size = window_size
        self.qk_norm = qk_norm
        self.cross_attn_norm = cross_attn_norm
        self.eps = eps

        # embeddings
        self.patch_embedding = nn.Conv3d(
            in_dim, dim, kernel_size=patch_size, stride=patch_size)
        self.text_embedding = nn.Sequential(
            nn.Linear(text_dim, dim), nn.GELU(approximate='tanh'),
            nn.Linear(dim, dim))

        self.time_embedding = nn.Sequential(
            nn.Linear(freq_dim, dim), nn.SiLU(), nn.Linear(dim, dim))
        self.time_projection = nn.Sequential(nn.SiLU(), nn.Linear(dim, dim * 6))

        # blocks
        cross_attn_type = 't2v_cross_attn' if model_type == 't2v' else 'i2v_cross_attn'
        self.blocks = nn.ModuleList([
            WanAttentionBlock(cross_attn_type, dim, ffn_dim, num_heads,
                              window_size, qk_norm, cross_attn_norm, eps)
            for _ in range(num_layers)
        ])

        # head
        self.head = Head(dim, out_dim, patch_size, eps)

        # buffers (don't use register_buffer otherwise dtype will be changed in to())
        assert (dim % num_heads) == 0 and (dim // num_heads) % 2 == 0
        d = dim // num_heads
        self.freqs = torch.cat([
            rope_params(1024, d - 4 * (d // 6)), # max_seq_len * (d // 6) * 2 * 2
            rope_params(1024, 2 * (d // 6)),
            rope_params(1024, 2 * (d // 6))
        ], dim=1) # max_seq_len * d // 2 * 2 * 2

        if model_type == 'i2v':
            self.img_emb = MLPProj(1280, dim)

        # initialize weights
        self.init_weights()

    def forward(
        self,
        x,
        t,
        context,
        seq_len,
        clip_fea=None,
        y=None,
    ):
        r"""
        Forward pass through the diffusion model

        Args:
            x (List[Tensor]):
                List of input video tensors, each with shape [C_in, F, H, W]
            t (Tensor):
                Diffusion timesteps tensor of shape [B]
            context (List[Tensor]):
                List of text embeddings each with shape [L, C]
            seq_len (`int`):
                Maximum sequence length for positional encoding
            clip_fea (Tensor, *optional*):
                CLIP image features for image-to-video mode
            y (List[Tensor], *optional*):
                Conditional video inputs for image-to-video mode, same shape as x

        Returns:
            List[Tensor]:
                List of denoised video tensors with original input shapes [C_out, F, H / 8, W / 8]
        """

        if self.model_type == 'i2v':
            assert clip_fea is not None and y is not None
        # params
        device = self.patch_embedding.weight.device
        if self.freqs.device != device:
            self.freqs = self.freqs.to(device)

        if y is not None:
            x = [torch.cat([u, v], dim=0) for u, v in zip(x, y)]

        # embeddings
        x = [self.patch_embedding(u.unsqueeze(0)) for u in x]
        grid_sizes = [vid.shape[2:] for vid in x]
        # grid_sizes = torch.stack(
        #     [torch.tensor(u.shape[2:], dtype=torch.long) for u in x])
        x = [u.flatten(2).transpose(1, 2) for u in x]
        seq_lens = [vid.shape[1] for vid in x]
        # seq_lens = torch.tensor([u.size(1) for u in x], dtype=torch.long)
        assert max(seq_lens) <= seq_len
        x = torch.cat([
            torch.cat([u, u.new_zeros(1, seq_len - u.size(1), u.size(2))],
                      dim=1) for u in x
        ])

        # time embeddings
        with amp.autocast(dtype=torch.float32):
            e = self.time_embedding(
                sinusoidal_embedding_1d(self.freq_dim, t).float())
            e0 = self.time_projection(e).unflatten(1, (6, self.dim))
            assert e.dtype == torch.float32 and e0.dtype == torch.float32

        # context
        context_lens = None
        context = self.text_embedding(
            torch.stack(
                [
                    torch.cat([u, u.new_zeros(self.text_len - u.size(0), u.size(1))])
                    for u in context
                ]
            )
        )

        if clip_fea is not None:
            context_clip = self.img_emb(clip_fea)  # bs x 257 x dim
            context = torch.concat([context_clip, context], dim=1)

        # arguments
        kwargs = dict(
            e=e0,
            seq_lens=seq_lens,
            grid_sizes=grid_sizes,
            freqs=self.freqs,
            context=context,
            context_lens=context_lens)

        with torch_profiler.record_function('wan_blocks'):
            # blocks
            for block in self.blocks:
                x = block(x, **kwargs)

        # head
        with torch_profiler.record_function('wan_head'):
            x = self.head(x, e)
        
        # unpatchify
        with torch_profiler.record_function('wan_unpatchify'):
            x = self.unpatchify(x, grid_sizes)
        return [u.float() for u in x]

    def unpatchify(self, x, grid_sizes):
        r"""
        Reconstruct video tensors from patch embeddings.

        Args:
            x (List[Tensor]):
                List of patchified features, each with shape [L, C_out * prod(patch_size)]
            grid_sizes (Tensor):
                Original spatial-temporal grid dimensions before patching,
                    shape [B, 3] (3 dimensions correspond to F_patches, H_patches, W_patches)

        Returns:
            List[Tensor]:
                Reconstructed video tensors with shape [C_out, F, H / 8, W / 8]
        """

        c = self.out_dim
        out = []
        for u, v in zip(x, grid_sizes):
            u = u[:math.prod(v)].view(*v, *self.patch_size, c)
            u = torch.einsum('fhwpqrc->cfphqwr', u)
            u = u.reshape(c, *[i * j for i, j in zip(v, self.patch_size)])
            out.append(u)
        return out

    def init_weights(self):
        r"""
        Initialize model parameters using Xavier initialization.
        """

        # basic init
        for m in self.modules():
            if isinstance(m, nn.Linear):
                nn.init.xavier_uniform_(m.weight)
                if m.bias is not None:
                    nn.init.zeros_(m.bias)

        # init embeddings
        nn.init.xavier_uniform_(self.patch_embedding.weight.flatten(1))
        for m in self.text_embedding.modules():
            if isinstance(m, nn.Linear):
                nn.init.normal_(m.weight, std=.02)
        for m in self.time_embedding.modules():
            if isinstance(m, nn.Linear):
                nn.init.normal_(m.weight, std=.02)

        # init output layer
        nn.init.zeros_(self.head.head.weight)
