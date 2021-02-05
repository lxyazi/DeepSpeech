# Copyright (c) 2021 PaddlePaddle Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import collections
import numpy as np
import paddle
from paddle import nn
from paddle.nn import functional as F
from paddle.nn import initializer as I


def brelu(x, t_min=0.0, t_max=24.0, name=None):
    return paddle.min(paddle.max(x, t_min), t_max)


def sequence_mask(x_len, max_len=None, dtype='float32'):
    max_len = (max_len or paddle.max(x))
    x_len = paddle.unsqueeze(x_len, -1)
    row_vector = paddle.arange(max_len)
    mask = row_vector < x_len
    mask = paddle.cast(mask, dtype)
    return mask


class ConvBn(nn.Layer):
    """Convolution layer with batch normalization.

    :param kernel_size: The x dimension of a filter kernel. Or input a tuple for
                        two image dimension.
    :type kernel_size: int|tuple|list
    :param num_channels_in: Number of input channels.
    :type num_channels_in: int
    :param num_channels_out: Number of output channels.
    :type num_channels_out: int
    :param stride: The x dimension of the stride. Or input a tuple for two 
                image dimension. 
    :type stride: int|tuple|list
    :param padding: The x dimension of the padding. Or input a tuple for two
                    image dimension.
    :type padding: int|tuple|list
    :param act: Activation type, relu|brelu
    :type act: string
    :param masks: Masks data layer to reset padding.
    :type masks: Variable
    :param name: Name of the layer.
    :param name: string
    :return: Batch norm layer after convolution layer.
    :rtype: Variable

    """

    def __init__(self, num_channels_in, num_channels_out, kernel_size, stride,
                 padding, act):

        super().__init__()
        self.kernel_size = kernel_size
        self.stride = stride
        self.padding = padding

        self.conv = nn.Conv2D(
            num_channels_in,
            num_channels_out,
            kernel_size=kernel_size,
            stride=stride,
            padding=padding,
            weight_attr=None,
            bias_attr=None,
            data_format='NCHW', )
        self.bn = nn.BatchNorm2D(
            num_channels=num_channels_out,
            param_attr=None,
            bias_attr=None,
            moving_mean_name=None,
            moving_variance_name=None,
            data_format='NCHW', )
        self.act = paddle.relu if act == 'relu' else brelu

    def forward(self, x, x_len):
        """
        x(Tensor): audio, shape [B, C, D, T]
        """
        x = self.conv(x)
        x = self.bn(x)
        x = self.act(x)

        # reset padding part to 0
        masks = sequence_mask(x_len)  #[B, T]
        masks = masks.unsqueeze(1).unsqueeze(1)  # [B, 1, 1, T]
        x = x.multiply(masks)

        x_len = (x_len - self.kernel_size[1] + 2 * self.padding[1]
                 ) // self.stride[1] + 1
        return x, x_len


class ConvStack(nn.Layer):
    """Convolution group with stacked convolution layers.

    :param feat_size: audio feature dim.
    :type feat_size: int
    :param num_stacks: Number of stacked convolution layers.
    :type num_stacks: int
    """

    def __init__(self, feat_size, num_stacks):
        super().__init__()
        self.feat_size = feat_size  # D
        self.num_stacks = num_stacks

        self.filter_size = (41, 11)  # [D, T]
        self.stride = (2, 3)
        self.padding = (20, 5)
        self.conv_in = ConvBn(
            num_channels_in=1,
            num_channels_out=32,
            kernel_size=self.filter_size,
            stride=self.stride,
            padding=self.padding,
            act='brelu', )
        self.conv_stack = nn.LayerList([
            ConvBn(
                num_channels_in=32,
                num_channels_out=32,
                kernel_size=(21, 11),
                stride=(2, 1),
                padding=(10, 5),
                act='brelu') for i in range(num_stacks - 1)
        ])

        # conv output feat_dim
        output_height = (feat_size - 1) // 2 + 1
        for i in range(self.num_stacks - 1):
            output_height = (output_height - 1) // 2 + 1
        self.output_height = output_height

    def forward(self, x, x_len):
        """
        x: shape [B, C, D, T]
        x_len : shape [B]
        """
        x, x_len = self.conv_in(x, x_len)
        for i, conv in enumerate(self.conv_stack):
            x, x_len = conv(x, x_len)
        return x, x_len


class RNNCell(nn.RNNCellBase):
    r"""
    Elman RNN (SimpleRNN) cell. Given the inputs and previous states, it 
    computes the outputs and updates states.
    The formula used is as follows:
    .. math::
        h_{t} & = act(x_{t} + b_{ih} + W_{hh}h_{t-1} + b_{hh})
        y_{t} & = h_{t}
    
    where :math:`act` is for :attr:`activation`.
    """

    def __init__(self,
                 hidden_size,
                 activation="tanh",
                 weight_ih_attr=None,
                 weight_hh_attr=None,
                 bias_ih_attr=None,
                 bias_hh_attr=None,
                 name=None):
        super().__init__()
        std = 1.0 / math.sqrt(hidden_size)
        self.weight_hh = self.create_parameter(
            (hidden_size, hidden_size),
            weight_hh_attr,
            default_initializer=I.Uniform(-std, std))
        self.bias_ih = self.create_parameter(
            (hidden_size, ),
            bias_ih_attr,
            is_bias=True,
            default_initializer=I.Uniform(-std, std))
        self.bias_hh = self.create_parameter(
            (hidden_size, ),
            bias_hh_attr,
            is_bias=True,
            default_initializer=I.Uniform(-std, std))

        self.hidden_size = hidden_size
        if activation not in ["tanh", "relu", "brelu"]:
            raise ValueError(
                "activation for SimpleRNNCell should be tanh or relu, "
                "but get {}".format(activation))
        self.activation = activation
        self._activation_fn = paddle.tanh \
            if activation == "tanh" \
            else F.relu
        if activation == 'brelu':
            self._activation_fn = brelu

    def forward(self, inputs, states=None):
        if states is None:
            states = self.get_initial_states(inputs, self.state_shape)
        pre_h = states
        i2h = inputs
        if self.bias_ih is not None:
            i2h += self.bias_ih
        h2h = paddle.matmul(pre_h, self.weight_hh, transpose_y=True)
        if self.bias_hh is not None:
            h2h += self.bias_hh
        h = self._activation_fn(i2h + h2h)
        return h, h

    @property
    def state_shape(self):
        return (self.hidden_size, )


class GRUCellShare(nn.RNNCellBase):
    r"""
    Gated Recurrent Unit (GRU) RNN cell. Given the inputs and previous states, 
    it computes the outputs and updates states.
    The formula for GRU used is as follows:
    ..  math::
        r_{t} & = \sigma(W_{ir}x_{t} + b_{ir} + W_{hr}h_{t-1} + b_{hr})
        z_{t} & = \sigma(W_{iz}x_{t} + b_{iz} + W_{hz}h_{t-1} + b_{hz})
        \widetilde{h}_{t} & = \tanh(W_{ic}x_{t} + b_{ic} + r_{t} * (W_{hc}h_{t-1} + b_{hc}))
        h_{t} & = z_{t} * h_{t-1} + (1 - z_{t}) * \widetilde{h}_{t}
        y_{t} & = h_{t}
    
    where :math:`\sigma` is the sigmoid fucntion, and * is the elemetwise 
    multiplication operator.
    """

    def __init__(self,
                 hidden_size,
                 weight_ih_attr=None,
                 weight_hh_attr=None,
                 bias_ih_attr=None,
                 bias_hh_attr=None,
                 name=None):
        super(GRUCell, self).__init__()
        std = 1.0 / math.sqrt(hidden_size)
        self.weight_hh = self.create_parameter(
            (3 * hidden_size, hidden_size),
            weight_hh_attr,
            default_initializer=I.Uniform(-std, std))
        self.bias_ih = self.create_parameter(
            (3 * hidden_size, ),
            bias_ih_attr,
            is_bias=True,
            default_initializer=I.Uniform(-std, std))
        self.bias_hh = self.create_parameter(
            (3 * hidden_size, ),
            bias_hh_attr,
            is_bias=True,
            default_initializer=I.Uniform(-std, std))

        self.hidden_size = hidden_size
        self.input_size = input_size
        self._gate_activation = F.sigmoid
        self._activation = paddle.tanh

    def forward(self, inputs, states=None):
        if states is None:
            states = self.get_initial_states(inputs, self.state_shape)

        pre_hidden = states
        x_gates = inputs
        if self.bias_ih is not None:
            x_gates = x_gates + self.bias_ih
        h_gates = paddle.matmul(pre_hidden, self.weight_hh, transpose_y=True)
        if self.bias_hh is not None:
            h_gates = h_gates + self.bias_hh

        x_r, x_z, x_c = paddle.split(x_gates, num_or_sections=3, axis=1)
        h_r, h_z, h_c = paddle.split(h_gates, num_or_sections=3, axis=1)

        r = self._gate_activation(x_r + h_r)
        z = self._gate_activation(x_z + h_z)
        c = self._activation(x_c + r * h_c)  # apply reset gate after mm
        h = (pre_hidden - c) * z + c

        return h, h

    @property
    def state_shape(self):
        r"""
        The `state_shape` of GRUCell is a shape `[hidden_size]` (-1 for batch
        size would be automatically inserted into shape). The shape corresponds
        to the shape of :math:`h_{t-1}`.
        """
        return (self.hidden_size, )


class BiRNNWithBN(nn.Layer):
    """Bidirectonal simple rnn layer with sequence-wise batch normalization.
    The batch normalization is only performed on input-state weights.

    :param name: Name of the layer parameters.
    :type name: string
    :param size: Dimension of RNN cells.
    :type size: int
    :param share_weights: Whether to share input-hidden weights between
                          forward and backward directional RNNs.
    :type share_weights: bool
    :return: Bidirectional simple rnn layer.
    :rtype: Variable
    """

    def __init__(self, i_size, h_size, share_weights):
        super().__init__()

        self.share_weights = share_weights
        self.pad_value = paddle.to_tensor(np.array([0.0], dtype=np.float32))
        if self.share_weights:
            #input-hidden weights shared between bi-directional rnn.
            self.fw_fc = nn.Linear(i_size, h_size)
            # batch norm is only performed on input-state projection
            self.fw_bn = nn.BatchNorm1D(h_size, data_format='NLC')
            self.bw_fc = self.fw_fc
            self.bw_bn = self.fw_bn
        else:
            self.fw_fc = nn.Linear(i_size, h_size)
            self.fw_bn = nn.BatchNorm1D(h_size, data_format='NLC')
            self.bw_fc = nn.Linear(i_size, h_size)
            self.bw_bn = nn.BatchNorm1D(h_size, data_format='NLC')

        self.fw_cell = RNNCell(hidden_size=h_size, activation='relu')
        self.bw_cell = RNNCell(
            hidden_size=h_size,
            activation='relu', )
        self.fw_rnn = nn.RNN(
            self.fw_cell, is_reverse=False, time_major=False)  #[B, T, D]
        self.bw_rnn = nn.RNN(
            self.fw_cell, is_reverse=True, time_major=False)  #[B, T, D]

    def forward(self, x, x_len):
        # x, shape [B, T, D]
        fw_x = self.fw_bn(self.fw_fc(x))
        bw_x = self.bw_bn(self.bw_bn(x))
        fw_x, _ = self.fw_rnn(inputs=fw_x, sequence_length=x_len)
        bw_x, _ = self.bw_rnn(inputs=bw_x, sequence_length=x_len)
        x = paddle.concat([fw_x, bw_x], axis=-1)
        return x, x_len


class BiGRUWithBN(nn.Layer):
    """Bidirectonal gru layer with sequence-wise batch normalization.
    The batch normalization is only performed on input-state weights.

    :param name: Name of the layer.
    :type name: string
    :param input: Input layer.
    :type input: Variable
    :param size: Dimension of GRU cells.
    :type size: int
    :param act: Activation type.
    :type act: string
    :return: Bidirectional GRU layer.
    :rtype: Variable
    """

    def __init__(self, i_size, act):
        super().__init__()
        hidden_size = i_size * 3
        self.fw_fc = nn.Linear(i_size, hidden_size)
        self.fw_bn = nn.BatchNorm1D(hidden_size, data_format='NLC')
        self.bw_fc = nn.Linear(i_size, hidden_size)
        self.bw_bn = nn.BatchNorm1D(hidden_size, data_format='NLC')

        self.fw_cell = GRUCellShare(hidden_size)
        self.bw_cell = GRUCellShare(hidden_size)
        self.fw_rnn = nn.RNN(
            self.fw_cell, is_reverse=False, time_major=False)  #[B, T, D]
        self.bw_rnn = nn.RNN(
            self.fw_cell, is_reverse=True, time_major=False)  #[B, T, D]

    def forward(self, x, x_len):
        # x, shape [B, T, D]
        fw_x = self.fw_bn(self.fw_fc(x))
        bw_x = self.bw_bn(self.bw_bn(x))
        fw_x, _ = self.fw_rnn(inputs=fw_x, sequence_length=x_len)
        bw_x, _ = self.bw_rnn(inputs=bw_x, sequence_length=x_len)
        x = paddle.concat([fw_x, bw_x], axis=-1)
        return x, x_len


class RNNStack(nn.Layer):
    """RNN group with stacked bidirectional simple RNN or GRU layers.

    :param input: Input layer.
    :type input: Variable
    :param size: Dimension of RNN cells in each layer.
    :type size: int
    :param num_stacks: Number of stacked rnn layers.
    :type num_stacks: int
    :param use_gru: Use gru if set True. Use simple rnn if set False.
    :type use_gru: bool
    :param share_rnn_weights: Whether to share input-hidden weights between
                              forward and backward directional RNNs.
                              It is only available when use_gru=False.
    :type share_weights: bool
    :return: Output layer of the RNN group.
    :rtype: Variable
    """

    def __init__(self, i_size, h_size, num_stacks, use_gru, share_rnn_weights):
        self.rnn_stacks = nn.LayerList()
        for i in range(num_stacks):
            if use_gru:
                #default:GRU using tanh
                self.rnn_stacks.append(BiGRUWithBN(size=i_size, act="relu"))
            else:
                self.rnn_stacks.append(
                    BiRNNWithBN(
                        i_size=i_size,
                        size=h_size,
                        share_weights=share_rnn_weights, ))

    def forward(self, x, x_len):
        """
        x: shape [B, T, D]
        x_len: shpae [B]
        """
        for i, rnn in enumerate(self.rnn_stacks):
            x, x_len = rnn(x, x_len)
        return x, x_len


class DeepSpeech2(nn.Layer):
    """The DeepSpeech2 network structure.

    :param audio_data: Audio spectrogram data layer.
    :type audio_data: Variable
    :param text_data: Transcription text data layer.
    :type text_data: Variable
    :param audio_len: Valid sequence length data layer.
    :type audio_len: Variable
    :param masks: Masks data layer to reset padding.
    :type masks: Variable
    :param dict_size: Dictionary size for tokenized transcription.
    :type dict_size: int
    :param num_conv_layers: Number of stacking convolution layers.
    :type num_conv_layers: int
    :param num_rnn_layers: Number of stacking RNN layers.
    :type num_rnn_layers: int
    :param rnn_size: RNN layer size (dimension of RNN cells).
    :type rnn_size: int
    :param use_gru: Use gru if set True. Use simple rnn if set False.
    :type use_gru: bool
    :param share_rnn_weights: Whether to share input-hidden weights between
                              forward and backward direction RNNs.
                              It is only available when use_gru=False.
    :type share_weights: bool
    :return: A tuple of an output unnormalized log probability layer (
             before softmax) and a ctc cost layer.
    :rtype: tuple of LayerOutput    
    """

    def __init__(self,
                 feat_size,
                 dict_size,
                 num_conv_layers=2,
                 num_rnn_layers=3,
                 rnn_size=256,
                 use_gru=False,
                 share_rnn_weight=True):
        super().__init__()
        self.feat_size = feat_size  # 161 for linear
        self.dict_size = dict_size

        self.conv = ConvStack(num_conv_layers)

        i_size = self.conv.output_height(feat_size)  # H after conv stack
        self.rnn = RNNStack(
            i_size=i_size,
            h_size=rnn_size,
            num_stacks=num_rnn_layers,
            use_gru=use_gru,
            share_rnn_weights=share_rnn_weights, )
        self.fc = nn.Linaer(rnn_size * 2, dict_size + 1)
        self.loss = nn.CTCLoss(blank=dict_size, reduction='none')

    def forward(self, audio, text, audio_len, text_len):
        """
        audio: shape [B, D, T]
        text: shape [B, T]
        audio_len: shape [B]
        text_len: shape [B]
        """
        # [B, D, T] -> [B, C=1, D, T]
        audio = audio.unsqueeze(1)

        # convolution group
        x, audio_len = self.conv(audio, audio_len)

        # convert data from convolution feature map to sequence of vectors
        B, C, D, T = paddle.shape(x)
        x = x.transpose([0, 3, 1, 2])  #[B, T, C, D]
        x = x.reshape([0, -1, C * D])  #[B, T, C*D]

        # remove padding part
        x, audio_len = self.rnn(x, audio_len)  #[B, T, D]

        logits = self.fc(x)  #[B, T, V + 1]

        #ctcdecoder need probs, not log_probs
        probs = F.log_softmax(logits)

        if not text:
            return probs, None
        else:
            # warp-ctc do softmax on activations
            # warp-ctc need activation with shape [T, B, V + 1]
            logits = logits.transpose([1, 0, 2])
            ctc_loss = self.loss(logits, text, audio_len, text_len)
            ctc_loss = paddle.reduce_sum(ctc_loss)
            return probs, ctc_loss