# Copyright 2017 Google Inc. All Rights Reserved.
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
# ==============================================================================

"""Basic sequence-to-sequence model with dynamic RNN support."""
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import abc

import tensorflow as tf

from tensorflow.python.layers import core as layers_core

from . import model_helper
from .utils import iterator_utils
from .utils import misc_utils as utils

utils.check_tensorflow_version()

__all__ = ["BaseModel", "Model"]


class BaseModel(object):
    """Sequence-to-sequence base class.
  """

    def __init__(self,
                 hparams,
                 mode,
                 iterator_s2s: iterator_utils.BatchedInput,
                 iterator_t2t: iterator_utils.BatchedInput,
                 iterator_s2t: iterator_utils.BatchedInput,
                 iterator_t2s: iterator_utils.BatchedInput,
                 source_vocab_table,
                 target_vocab_table,
                 reverse_source_vocab_table=None,
                 reverse_target_vocab_table=None,
                 scope=None,
                 extra_args=None):
        """Create the model.

    Args:
      hparams: Hyperparameter configurations.
      mode: TRAIN | EVAL | INFER
      iterator: Dataset Iterator that feeds data.
      source_vocab_table: Lookup table mapping source words to ids.
      target_vocab_table: Lookup table mapping target words to ids.
      reverse_target_vocab_table: Lookup table mapping ids to target words. Only
        required in INFER mode. Defaults to None.
      scope: scope of the model.
      extra_args: model_helper.ExtraArgs, for passing customizable functions.

    """
        assert isinstance(iterator_s2s, iterator_utils.BatchedInput)
        assert isinstance(iterator_t2t, iterator_utils.BatchedInput)
        assert isinstance(iterator_s2t, iterator_utils.BatchedInput)
        assert isinstance(iterator_t2s, iterator_utils.BatchedInput)
        self.iterator_s2s = iterator_s2s
        self.iterator_t2t = iterator_t2t
        self.iterator_s2t = iterator_s2t
        self.iterator_t2s = iterator_t2s

        self.mode = mode
        self.src_vocab_table = source_vocab_table
        self.tgt_vocab_table = target_vocab_table

        self.src_vocab_size = hparams.src_vocab_size
        self.tgt_vocab_size = hparams.tgt_vocab_size
        self.num_gpus = hparams.num_gpus
        self.time_major = hparams.time_major

        # self.iterator_trans_src = iterator_trans_src
        # self.iterator_trans_tgt = iterator_trans_tgt
        #if self.mode == tf.contrib.learn.ModeKeys.TRAIN:
            # self.original_funcs_src_ = tf.placeholder(iterator_src.source.dtype, shape=iterator_src.source.shape)
            # self.original_funcs_tgt_ = tf.placeholder(iterator_tgt.source.dtype, shape=iterator_tgt.source.shape)
            # self.translated_funcs_src_ = tf.placeholder(iterator_src.source.dtype, shape=iterator_src.source.shape)
            # self.translated_funcs_tgt_ = tf.placeholder(iterator_tgt.source.dtype, shape=iterator_tgt.source.shape)

        # extra_args: to make it flexible for adding external customizable code
        self.single_cell_fn = None
        if extra_args:
            self.single_cell_fn = extra_args.single_cell_fn

        # Set num layers
        self.num_encoder_layers = hparams.num_encoder_layers
        self.num_decoder_layers = hparams.num_decoder_layers
        assert self.num_encoder_layers
        assert self.num_decoder_layers

        # Set num residual layers
        if hasattr(hparams, "num_residual_layers"):  # compatible common_test_utils
            self.num_encoder_residual_layers = hparams.num_residual_layers
            self.num_decoder_residual_layers = hparams.num_residual_layers
        else:
            self.num_encoder_residual_layers = hparams.num_encoder_residual_layers
            self.num_decoder_residual_layers = hparams.num_decoder_residual_layers

        # Initializer
        initializer = model_helper.get_initializer(
            hparams.init_op, hparams.random_seed, hparams.init_weight)
        tf.get_variable_scope().set_initializer(initializer)

        # Embeddings
        self.init_embeddings(hparams, scope)

        # TODO: iterator_tgt
        self.batch_size = tf.size(self.iterator_s2s.source_sequence_length)

        # Projection
        with tf.variable_scope(scope or "build_network"):
            with tf.variable_scope("decoder/output_projection"):
                self.output_layer_src = layers_core.Dense(
                    hparams.src_vocab_size, use_bias=False, name="output_projection_src")
                self.output_layer_tgt = layers_core.Dense(
                    hparams.tgt_vocab_size, use_bias=False, name="output_projection_tgt")

        # Train graph
        src2src, tgt2tgt, src2tgt, tgt2src, train_loss_ae, train_loss_D = \
            self.build_graph(hparams, scope=scope)

        if self.mode == tf.contrib.learn.ModeKeys.TRAIN:
            self.train_loss_ae = train_loss_ae
            self.train_loss_D = train_loss_D
            self.word_count_s2s = tf.reduce_sum(
                self.iterator_s2s.source_sequence_length) + tf.reduce_sum(
                self.iterator_s2s.target_sequence_length)
        elif self.mode == tf.contrib.learn.ModeKeys.EVAL:
            self.eval_loss = train_loss_ae
        elif self.mode == tf.contrib.learn.ModeKeys.INFER:
            self.infer_logits_src, self.final_context_state_src, self.sample_id_src = src2src
            self.infer_logits_tgt, self.final_context_state_tgt, self.sample_id_tgt = tgt2tgt
            (self.infer_cross_logits_src, self.final_context_state_cross_src, self.sample_id_cross_src) = tgt2src
            (self.infer_cross_logits_tgt, self.final_context_state_cross_tgt, self.sample_id_cross_tgt) = src2tgt

            self.sample_words_src = reverse_source_vocab_table.lookup(tf.to_int64(self.sample_id_src))
            self.sample_words_tgt = reverse_target_vocab_table.lookup(tf.to_int64(self.sample_id_tgt))
            self.sample_words_src_cross = reverse_source_vocab_table.lookup(tf.to_int64(self.sample_id_cross_src))
            self.sample_words_tgt_cross = reverse_target_vocab_table.lookup(tf.to_int64(self.sample_id_cross_tgt))

        if self.mode != tf.contrib.learn.ModeKeys.INFER:
            ## Count the number of predicted words for compute ppl.
            self.predict_count_s2s = tf.reduce_sum(
                self.iterator_s2s.target_sequence_length)
            self.predict_count_t2t = tf.reduce_sum(
                self.iterator_t2t.target_sequence_length)

        self.global_step = tf.Variable(0, trainable=False)

        params_D = tf.trainable_variables(scope='.*discriminator.*')
        params_ae = list(set(tf.trainable_variables()) - set(params_D))

        # Gradients and SGD update operation for training the model.
        # Arrage for the embedding vars to appear at the beginning.
        if self.mode == tf.contrib.learn.ModeKeys.TRAIN:
            self.learning_rate = tf.constant(hparams.learning_rate)
            # warm-up
            self.learning_rate = self._get_learning_rate_warmup(hparams)
            # decay
            self.learning_rate = self._get_learning_rate_decay(hparams)

            def train_params(train_vars, train_loss):
                # Optimizer
                if hparams.optimizer == "sgd":
                    opt = tf.train.GradientDescentOptimizer(self.learning_rate)
                    tf.summary.scalar("lr", self.learning_rate)
                elif hparams.optimizer == "adam":
                    opt = tf.train.AdamOptimizer(self.learning_rate)

                # Gradients
                gradients = tf.gradients(
                    train_loss,
                    train_vars,
                    colocate_gradients_with_ops=hparams.colocate_gradients_with_ops)

                clipped_grads, grad_norm_summary, grad_norm = model_helper.gradient_clip(
                    gradients, max_gradient_norm=hparams.max_gradient_norm)
                grad_norm = grad_norm

                update = opt.apply_gradients(
                    zip(clipped_grads, train_vars), global_step=self.global_step)

                # Summary
                train_summary = tf.summary.merge([
                                                     tf.summary.scalar("lr", self.learning_rate),
                                                     tf.summary.scalar("train_loss", train_loss),
                                                 ] + grad_norm_summary)
                return grad_norm, update, train_summary

            self.grad_norm_ae, self.update_ae, self.train_summary = train_params(params_ae, self.train_loss_ae)
            self.grad_norm_D, self.update_D, self.train_summary_D = train_params(params_D, self.train_loss_D)
            self.train_summary = tf.summary.merge([self.train_summary, self.train_summary_D])

        if self.mode == tf.contrib.learn.ModeKeys.INFER:
            self.infer_summary_src, self.infer_summary_tgt = self._get_infer_summary(hparams)
            self.infer_summary_src_cross, self.infer_summary_tgt_cross = self._get_infer_summary_cross(hparams)

        # Saver
        self.saver = tf.train.Saver(tf.global_variables(), max_to_keep=hparams.num_keep_ckpts)

        # Print trainable variables
        utils.print_out("# Trainable variables")

        utils.print_out('AutoEncoder params')
        for param in params_ae:
            utils.print_out("  %s, %s, %s" % (param.name, str(param.get_shape()),
                                              param.op.device))
        utils.print_out('Discriminator params')
        for param in params_D:
            utils.print_out("  %s, %s, %s" % (param.name, str(param.get_shape()),
                                              param.op.device))

    def _get_learning_rate_warmup(self, hparams):
        """Get learning rate warmup."""
        warmup_steps = hparams.warmup_steps
        warmup_scheme = hparams.warmup_scheme
        utils.print_out("  learning_rate=%g, warmup_steps=%d, warmup_scheme=%s" %
                        (hparams.learning_rate, warmup_steps, warmup_scheme))

        # Apply inverse decay if global steps less than warmup steps.
        # Inspired by https://arxiv.org/pdf/1706.03762.pdf (Section 5.3)
        # When step < warmup_steps,
        #   learing_rate *= warmup_factor ** (warmup_steps - step)
        if warmup_scheme == "t2t":
            # 0.01^(1/warmup_steps): we start with a lr, 100 times smaller
            warmup_factor = tf.exp(tf.log(0.01) / warmup_steps)
            inv_decay = warmup_factor ** (
                tf.to_float(warmup_steps - self.global_step))
        else:
            raise ValueError("Unknown warmup scheme %s" % warmup_scheme)

        return tf.cond(
            self.global_step < hparams.warmup_steps,
            lambda: inv_decay * self.learning_rate,
            lambda: self.learning_rate,
            name="learning_rate_warump_cond")

    def _get_learning_rate_decay(self, hparams):
        """Get learning rate decay."""
        if hparams.decay_scheme in ["luong5", "luong10", "luong234"]:
            decay_factor = 0.5
            if hparams.decay_scheme == "luong5":
                start_decay_step = int(hparams.num_train_steps / 2)
                decay_times = 5
            elif hparams.decay_scheme == "luong10":
                start_decay_step = int(hparams.num_train_steps / 2)
                decay_times = 10
            elif hparams.decay_scheme == "luong234":
                start_decay_step = int(hparams.num_train_steps * 2 / 3)
                decay_times = 4
            remain_steps = hparams.num_train_steps - start_decay_step
            decay_steps = int(remain_steps / decay_times)
        elif not hparams.decay_scheme:  # no decay
            start_decay_step = hparams.num_train_steps
            decay_steps = 0
            decay_factor = 1.0
        elif hparams.decay_scheme:
            raise ValueError("Unknown decay scheme %s" % hparams.decay_scheme)
        utils.print_out("  decay_scheme=%s, start_decay_step=%d, decay_steps %d, "
                        "decay_factor %g" % (hparams.decay_scheme,
                                             start_decay_step,
                                             decay_steps,
                                             decay_factor))

        return tf.cond(
            self.global_step < start_decay_step,
            lambda: self.learning_rate,
            lambda: tf.train.exponential_decay(
                self.learning_rate,
                (self.global_step - start_decay_step),
                decay_steps, decay_factor, staircase=True),
            name="learning_rate_decay_cond")

    def init_embeddings(self, hparams, scope):
        """Init embeddings."""
        self.embedding_src, self.embedding_tgt = (
            model_helper.create_emb_for_encoder_and_decoder(
                share_vocab=hparams.share_vocab,
                src_vocab_size=self.src_vocab_size,
                tgt_vocab_size=self.tgt_vocab_size,
                src_embed_size=hparams.num_units,
                tgt_embed_size=hparams.num_units,
                num_partitions=hparams.num_embeddings_partitions,
                src_vocab_file=hparams.src_vocab_file,
                tgt_vocab_file=hparams.tgt_vocab_file,
                src_embed_file=hparams.src_embed_file,
                tgt_embed_file=hparams.tgt_embed_file,
                scope=scope, ))
        # self.embedding_decoder = self.embedding_encoder

    def train(self, sess):#,
              #original_funcs_src, translated_funcs_src,
              #original_funcs_tgt, translated_funcs_tgt):
        assert self.mode == tf.contrib.learn.ModeKeys.TRAIN
        # TODO: check for predict_count_tgt & word_count_tgt
        res_ae = sess.run([self.update_ae,
                           self.train_loss_ae,
                           self.predict_count_s2s,
                           self.train_summary,
                           self.global_step,
                           self.word_count_s2s,
                           self.batch_size,
                           self.grad_norm_ae,
                           self.learning_rate])
                          # feed_dict={
                          #     self.original_funcs_src_: original_funcs_src,
                          #     self.translated_funcs_src_: translated_funcs_src,
                          #     self.original_funcs_tgt_: original_funcs_tgt,
                          #     self.translated_funcs_tgt_: translated_funcs_tgt,
                          # })
        res_D = sess.run([self.update_D,
                          self.train_loss_D,
                          self.train_summary_D])
        return res_ae, res_D

    def eval(self, sess):
        assert self.mode == tf.contrib.learn.ModeKeys.EVAL
        # TODO: predict_count_tgt
        return sess.run([self.eval_loss,
                         self.predict_count_s2s,
                         self.batch_size])

    def build_graph(self, hparams, scope=None):
        """Subclass must implement this method.

    Creates a sequence-to-sequence model with dynamic RNN decoder API.
    Args:
      hparams: Hyperparameter configurations.
      scope: VariableScope for the created subgraph; default "dynamic_seq2seq".

    Returns:
      A tuple of the form (logits, loss, final_context_state),
      where:
        logits: float32 Tensor [batch_size x num_decoder_symbols].
        loss: the total loss / batch_size.
        final_context_state: The final state of decoder RNN.

    Raises:
      ValueError: if encoder_type differs from mono and bi, or
        attention_option is not (luong | scaled_luong |
        bahdanau | normed_bahdanau).
    """
        utils.print_out("# creating %s graph ..." % self.mode)
        dtype = tf.float32

        with tf.variable_scope(scope or "dynamic_seq2seq", dtype=dtype):
            ############################
            ########### AUTO ###########
            ############################
            with tf.variable_scope(scope or "auto", dtype=dtype):
                # Encoder
                encoder_outputs_s2s, encoder_state_s2s = self._build_encoder(
                    hparams, iterator=self.iterator_s2s, embedding=self.embedding_src)
                encoder_outputs_t2t, encoder_state_t2t = self._build_encoder(
                    hparams, iterator=self.iterator_t2t, embedding=self.embedding_tgt)

                # Discriminator
                discriminator_logits_s2s, _ = \
                    self._build_discriminator(encoder_outputs_s2s)
                discriminator_logits_t2t, _ = \
                    self._build_discriminator(encoder_outputs_t2t)

                # Decoder
                logits_s2s, sample_id_s2s, final_context_state_s2s = self._build_decoder(
                    encoder_outputs_s2s, encoder_state_s2s, hparams,
                    iterator=self.iterator_s2s, embedding=self.embedding_src,
                    vocab_table=self.src_vocab_table, output_layer=self.output_layer_src,
                    sos=hparams.sos_2src)

                logits_t2t, sample_id_t2t, final_context_state_t2t = self._build_decoder(
                    encoder_outputs_t2t, encoder_state_t2t, hparams,
                    iterator=self.iterator_t2t, embedding=self.embedding_tgt,
                    vocab_table=self.tgt_vocab_table, output_layer=self.output_layer_tgt,
                    sos=hparams.sos_2tgt)

            with tf.variable_scope(scope or "cross", dtype=dtype):
                ###################################
                ############ CROSS ################
                ###################################
                # Encoder
                encoder_outputs_s2t, encoder_state_s2t = self._build_encoder(
                    hparams, iterator=self.iterator_s2t, embedding=self.embedding_src)
                encoder_outputs_t2s, encoder_state_t2s = self._build_encoder(
                    hparams, iterator=self.iterator_t2s, embedding=self.embedding_tgt)

                # Discriminator
                discriminator_logits_s2t, _ = \
                    self._build_discriminator(encoder_outputs_s2t)
                discriminator_logits_t2s, _ = \
                    self._build_discriminator(encoder_outputs_t2s)

                logits_s2t, sample_id_s2t, final_context_state_s2t = self._build_decoder(
                    encoder_outputs_s2t, encoder_state_s2t, hparams,
                    iterator=self.iterator_s2t, embedding=self.embedding_tgt,
                    vocab_table=self.tgt_vocab_table, output_layer=self.output_layer_tgt,
                    sos=hparams.sos_2tgt)

                logits_t2s, sample_id_t2s, final_context_state_t2s = self._build_decoder(
                    encoder_outputs_t2s, encoder_state_t2s, hparams,
                    iterator=self.iterator_t2s, embedding=self.embedding_src,
                    vocab_table=self.src_vocab_table, output_layer=self.output_layer_src,
                    sos=hparams.sos_2src)

            # Loss
            if self.mode != tf.contrib.learn.ModeKeys.INFER:
                with tf.device(model_helper.get_device_str(self.num_encoder_layers - 1,
                                                           self.num_gpus)):
                    loss_auto_s2s = self._compute_loss(logits_s2s, self.iterator_s2s)
                    loss_auto_t2t = self._compute_loss(logits_t2t, self.iterator_t2t)

                    loss_cross_s2t = self._compute_loss(logits_s2t, self.iterator_s2t)
                    loss_cross_t2s = self._compute_loss(logits_t2s, self.iterator_t2s)

                    D_labels_s2s = tf.zeros_like(self.iterator_s2s.source)
                    loss_D_s2s = self._compute_discriminator_loss(discriminator_logits_s2s,
                                                                  D_labels_s2s)
                    D_labels_s2t = tf.zeros_like(self.iterator_s2t.source)
                    loss_D_s2t = self._compute_discriminator_loss(discriminator_logits_s2t,
                                                                  D_labels_s2t)
                    D_labels_t2t = tf.ones_like(self.iterator_t2t.source)
                    loss_D_t2t = self._compute_discriminator_loss(discriminator_logits_t2t,
                                                                  D_labels_t2t)
                    D_labels_t2s = tf.ones_like(self.iterator_t2s.source)
                    loss_D_t2s = self._compute_discriminator_loss(discriminator_logits_t2s,
                                                                  D_labels_t2s)

                    # adv
                    loss_adv_s2s = self._compute_discriminator_loss(discriminator_logits_s2s,
                                                                    1 - D_labels_s2s)
                    loss_adv_s2t = self._compute_discriminator_loss(discriminator_logits_s2t,
                                                                    1 - D_labels_s2t)
                    loss_adv_t2t = self._compute_discriminator_loss(discriminator_logits_t2t,
                                                                    1 - D_labels_t2t)
                    loss_adv_t2s = self._compute_discriminator_loss(discriminator_logits_t2s,
                                                                    1 - D_labels_t2s)
                    loss_adv = tf.add_n([loss_adv_s2s, loss_adv_s2t, loss_adv_t2t, loss_adv_t2s], name='loss_adv')
                    loss_auto_total = tf.add(loss_auto_s2s, loss_auto_t2t, 'total_auto_loss')
                    loss_cross_total = tf.add(loss_cross_s2t, loss_cross_t2s, 'total_cross_loss')
                    loss_D_total = tf.add_n([loss_D_s2s, loss_D_s2t, loss_D_t2t, loss_D_t2s], 'total_D_loss')
                    loss = tf.add_n([loss_auto_total, loss_D_total, loss_cross_total], 'total_loss')
            else:
                loss = None
                loss_adv = None

            return (logits_s2s, final_context_state_s2s, sample_id_s2s), \
                   (logits_t2t, final_context_state_t2t, sample_id_t2t), \
                   (logits_s2t, final_context_state_s2t, sample_id_s2t), \
                   (logits_t2s, final_context_state_t2s, sample_id_t2s), \
                   loss, loss_adv

    @abc.abstractmethod
    def _build_encoder(self, hparams, iterator, embedding) -> tuple:
        """Subclass must implement this.

    Build and run an RNN encoder.

    Args:
      hparams: Hyperparameters configurations.

    Returns:
      A tuple of encoder_outputs and encoder_state.
    """
        pass

    def _build_encoder_cell(self, hparams, num_layers, num_residual_layers,
                            base_gpu=0):
        """Build a multi-layer RNN cell that can be used by encoder."""

        return model_helper.create_rnn_cell(
            unit_type=hparams.unit_type,
            num_units=hparams.num_units,
            num_layers=num_layers,
            num_residual_layers=num_residual_layers,
            forget_bias=hparams.forget_bias,
            dropout=hparams.dropout,
            num_gpus=hparams.num_gpus,
            mode=self.mode,
            base_gpu=base_gpu,
            single_cell_fn=self.single_cell_fn)

    def _get_infer_maximum_iterations(self, hparams, source_sequence_length):
        """Maximum decoding steps at inference time."""
        if hparams.tgt_max_len_infer:
            maximum_iterations = hparams.tgt_max_len_infer
            utils.print_out("  decoding maximum_iterations %d" % maximum_iterations)
        else:
            # TODO(thangluong): add decoding_length_factor flag
            decoding_length_factor = 2.0
            max_encoder_length = tf.reduce_max(source_sequence_length)
            maximum_iterations = tf.to_int32(tf.round(
                tf.to_float(max_encoder_length) * decoding_length_factor))
        return maximum_iterations

    def _build_decoder(self, encoder_outputs, encoder_state, hparams,
                       iterator, embedding, vocab_table, output_layer, sos):
        """Build and run a RNN decoder with a final projection layer.

    Args:
      encoder_outputs: The outputs of encoder for every time step.
      encoder_state: The final state of the encoder.
      hparams: The Hyperparameters configurations.

    Returns:
      A tuple of final logits and final decoder state:
        logits: size [time, batch_size, vocab_size] when time_major=True.
    """
        tgt_sos_id = tf.cast(vocab_table.lookup(tf.constant(sos)),
                             tf.int32)
        tgt_eos_id = tf.cast(vocab_table.lookup(tf.constant(hparams.eos)),
                             tf.int32)

        # maximum_iteration: The maximum decoding steps.
        maximum_iterations = self._get_infer_maximum_iterations(
            hparams, iterator.source_sequence_length)

        ## Decoder.
        with tf.variable_scope("decoder", reuse=tf.AUTO_REUSE) as decoder_scope:
            cell, decoder_initial_state = self._build_decoder_cell(
                hparams, encoder_outputs, encoder_state,
                iterator.source_sequence_length)

            ## Train or eval
            if self.mode != tf.contrib.learn.ModeKeys.INFER:
                # decoder_emp_inp: [max_time, batch_size, num_units]
                target_input = iterator.target_input
                if self.time_major:
                    target_input = tf.transpose(target_input)
                decoder_emb_inp = tf.nn.embedding_lookup(
                    embedding, target_input)

                # Helper
                helper = tf.contrib.seq2seq.TrainingHelper(
                    decoder_emb_inp, iterator.target_sequence_length,
                    time_major=self.time_major)

                # Decoder
                my_decoder = tf.contrib.seq2seq.BasicDecoder(
                    cell,
                    helper,
                    decoder_initial_state, )

                # Dynamic decoding
                outputs, final_context_state, _ = tf.contrib.seq2seq.dynamic_decode(
                    my_decoder,
                    output_time_major=self.time_major,
                    swap_memory=True,
                    scope=decoder_scope)

                sample_id = outputs.sample_id

                # Note: there's a subtle difference here between train and inference.
                # We could have set output_layer when create my_decoder
                #   and shared more code between train and inference.
                # We chose to apply the output_layer to all timesteps for speed:
                #   10% improvements for small models & 20% for larger ones.
                # If memory is a concern, we should apply output_layer per timestep.
                logits = output_layer(outputs.rnn_output)

            ## Inference
            else:
                beam_width = hparams.beam_width
                length_penalty_weight = hparams.length_penalty_weight
                start_tokens = tf.fill([self.batch_size], tgt_sos_id)
                end_token = tgt_eos_id

                if beam_width > 0:
                    my_decoder = tf.contrib.seq2seq.BeamSearchDecoder(
                        cell=cell,
                        embedding=embedding,
                        start_tokens=start_tokens,
                        end_token=end_token,
                        initial_state=decoder_initial_state,
                        beam_width=beam_width,
                        output_layer=output_layer,
                        length_penalty_weight=length_penalty_weight)
                else:
                    # Helper
                    sampling_temperature = hparams.sampling_temperature
                    if sampling_temperature > 0.0:
                        helper = tf.contrib.seq2seq.SampleEmbeddingHelper(
                            embedding, start_tokens, end_token,
                            softmax_temperature=sampling_temperature,
                            seed=hparams.random_seed)
                    else:
                        helper = tf.contrib.seq2seq.GreedyEmbeddingHelper(
                            embedding, start_tokens, end_token)

                    # Decoder
                    my_decoder = tf.contrib.seq2seq.BasicDecoder(
                        cell,
                        helper,
                        decoder_initial_state,
                        output_layer=output_layer  # applied per timestep
                    )

                # Dynamic decoding
                outputs, final_context_state, _ = tf.contrib.seq2seq.dynamic_decode(
                    my_decoder,
                    maximum_iterations=maximum_iterations,
                    output_time_major=self.time_major,
                    swap_memory=True,
                    scope=decoder_scope)

                if beam_width > 0:
                    logits = tf.no_op()
                    sample_id = outputs.predicted_ids
                else:
                    logits = outputs.rnn_output
                    sample_id = outputs.sample_id

        return logits, sample_id, final_context_state

    def get_max_time(self, tensor):
        time_axis = 0 if self.time_major else 1
        return tensor.shape[time_axis].value or tf.shape(tensor)[time_axis]

    @abc.abstractmethod
    def _build_decoder_cell(self, hparams, encoder_outputs, encoder_state,
                            source_sequence_length) -> tuple:
        """Subclass must implement this.

    Args:
      hparams: Hyperparameters configurations.
      encoder_outputs: The outputs of encoder for every time step.
      encoder_state: The final state of the encoder.
      source_sequence_length: sequence length of encoder_outputs.

    Returns:
      A tuple of a multi-layer RNN cell used by decoder
        and the intial state of the decoder RNN.
    """
        pass

    def _compute_loss(self, logits, iterator):
        """Compute optimization loss."""
        target_output = iterator.target_output
        if self.time_major:
            target_output = tf.transpose(target_output)
        max_time = self.get_max_time(target_output)
        crossent = tf.nn.sparse_softmax_cross_entropy_with_logits(
            labels=target_output, logits=logits)
        target_weights = tf.sequence_mask(
            iterator.target_sequence_length, max_time, dtype=logits.dtype)
        if self.time_major:
            target_weights = tf.transpose(target_weights)

        loss = tf.reduce_sum(
            crossent * target_weights) / tf.to_float(self.batch_size)
        return loss

    def _get_infer_summary(self, hparams):
        return tf.no_op(), tf.no_op()

    def _get_infer_summary_cross(self, hparams):
        return tf.no_op(), tf.no_op()

    def infer_cross(self, sess):
        assert self.mode == tf.contrib.learn.ModeKeys.INFER
        return sess.run([
            (self.infer_cross_logits_src, self.infer_summary_src_cross, self.sample_id_cross_src, self.sample_words_src_cross),
            (self.infer_cross_logits_tgt, self.infer_summary_tgt_cross, self.sample_id_cross_tgt, self.sample_words_tgt_cross)
        ])

    def infer(self, sess):
        assert self.mode == tf.contrib.learn.ModeKeys.INFER
        return sess.run([
            (self.infer_logits_src, self.infer_summary_src, self.sample_id_src, self.sample_words_src),
            (self.infer_logits_tgt, self.infer_summary_tgt, self.sample_id_tgt, self.sample_words_tgt)
        ])

    def infer_and_source(self, sess):
        assert self.mode == tf.contrib.learn.ModeKeys.INFER
        return sess.run([
            (self.iterator_src.source, self.infer_logits_src, self.infer_summary_src, self.sample_id_src,
             self.sample_words_src),
            (self.iterator_tgt.source, self.infer_logits_tgt, self.infer_summary_tgt, self.sample_id_tgt,
             self.sample_words_tgt)
        ])

    def decode_cross(self, sess):
        (_, infer_summary_src, _, sample_words_src), (_, infer_summary_tgt, _, sample_words_tgt) = self.infer_cross(sess)

        # make sure outputs is of shape [batch_size, time] or [beam_width,
        # batch_size, time] when using beam search.
        if self.time_major:
            sample_words_src = sample_words_src.transpose()
            sample_words_tgt = sample_words_tgt.transpose()
        elif sample_words_src.ndim == 3:  # beam search output in [batch_size, time, beam_width] shape.
            sample_words_src = sample_words_src.transpose([2, 0, 1])
            sample_words_tgt = sample_words_tgt.transpose([2, 0, 1])
        return (sample_words_src, infer_summary_src), (sample_words_tgt, infer_summary_tgt)

    def decode(self, sess):
        """Decode a batch.

    Args:
      sess: tensorflow session to use.

    Returns:
      A tuple consiting of outputs, infer_summary.
        outputs: of size [batch_size, time]
    """
        (_, infer_summary_src, _, sample_words_src), (_, infer_summary_tgt, _, sample_words_tgt) = self.infer(sess)

        # make sure outputs is of shape [batch_size, time] or [beam_width,
        # batch_size, time] when using beam search.
        if self.time_major:
            sample_words_src = sample_words_src.transpose()
            sample_words_tgt = sample_words_tgt.transpose()
        elif sample_words_src.ndim == 3:  # beam search output in [batch_size, time, beam_width] shape.
            sample_words_src = sample_words_src.transpose([2, 0, 1])
            sample_words_tgt = sample_words_tgt.transpose([2, 0, 1])
        return (sample_words_src, infer_summary_src), (sample_words_tgt, infer_summary_tgt)

    def _build_discriminator(self, outputs):
        with tf.variable_scope("discriminator", reuse=tf.AUTO_REUSE):
            outputs = tf.layers.dense(outputs, 1024, activation=tf.nn.tanh, name='dense1_D')
            outputs = tf.layers.dense(outputs, 1024, activation=tf.nn.tanh, name='dense2_D')
            outputs = tf.layers.dense(outputs, 2, activation=tf.nn.tanh, name='dense_last_D')
        return outputs, tf.nn.softmax(outputs)

    def _compute_discriminator_loss(self, logits, labels):
        return tf.losses.sparse_softmax_cross_entropy(logits=logits,
                                                      labels=labels)


class Model(BaseModel):
    """Sequence-to-sequence dynamic model.

  This class implements a multi-layer recurrent neural network as encoder,
  and a multi-layer recurrent neural network decoder.
  """

    def _build_encoder(self, hparams, iterator, embedding) -> tuple:
        """Build an encoder."""
        num_layers = self.num_encoder_layers
        num_residual_layers = self.num_encoder_residual_layers

        source = iterator.source
        if self.time_major:
            source = tf.transpose(source)

        with tf.variable_scope("encoder") as scope:
            dtype = scope.dtype
            # Look up embedding, emp_inp: [max_time, batch_size, num_units]
            encoder_emb_inp = tf.nn.embedding_lookup(
                self.embedding_encoder, source)

            # Encoder_outputs: [max_time, batch_size, num_units]
            if hparams.encoder_type == "uni":
                utils.print_out("  num_layers = %d, num_residual_layers=%d" %
                                (num_layers, num_residual_layers))
                cell = self._build_encoder_cell(
                    hparams, num_layers, num_residual_layers)

                encoder_outputs, encoder_state = tf.nn.dynamic_rnn(
                    cell,
                    encoder_emb_inp,
                    dtype=dtype,
                    sequence_length=iterator.source_sequence_length,
                    time_major=self.time_major,
                    swap_memory=True)
            elif hparams.encoder_type == "bi":
                num_bi_layers = int(num_layers / 2)
                num_bi_residual_layers = int(num_residual_layers / 2)
                utils.print_out("  num_bi_layers = %d, num_bi_residual_layers=%d" %
                                (num_bi_layers, num_bi_residual_layers))

                encoder_outputs, bi_encoder_state = (
                    self._build_bidirectional_rnn(
                        inputs=encoder_emb_inp,
                        sequence_length=iterator.source_sequence_length,
                        dtype=dtype,
                        hparams=hparams,
                        num_bi_layers=num_bi_layers,
                        num_bi_residual_layers=num_bi_residual_layers))

                if num_bi_layers == 1:
                    encoder_state = bi_encoder_state
                else:
                    # alternatively concat forward and backward states
                    encoder_state = []
                    for layer_id in range(num_bi_layers):
                        encoder_state.append(bi_encoder_state[0][layer_id])  # forward
                        encoder_state.append(bi_encoder_state[1][layer_id])  # backward
                    encoder_state = tuple(encoder_state)
            else:
                raise ValueError("Unknown encoder_type %s" % hparams.encoder_type)
        return encoder_outputs, encoder_state

    def _build_bidirectional_rnn(self, inputs, sequence_length,
                                 dtype, hparams,
                                 num_bi_layers,
                                 num_bi_residual_layers,
                                 base_gpu=0):
        """Create and call biddirectional RNN cells.

    Args:
      num_residual_layers: Number of residual layers from top to bottom. For
        example, if `num_bi_layers=4` and `num_residual_layers=2`, the last 2 RNN
        layers in each RNN cell will be wrapped with `ResidualWrapper`.
      base_gpu: The gpu device id to use for the first forward RNN layer. The
        i-th forward RNN layer will use `(base_gpu + i) % num_gpus` as its
        device id. The `base_gpu` for backward RNN cell is `(base_gpu +
        num_bi_layers)`.

    Returns:
      The concatenated bidirectional output and the bidirectional RNN cell"s
      state.
    """
        # Construct forward and backward cells
        fw_cell = self._build_encoder_cell(hparams,
                                           num_bi_layers,
                                           num_bi_residual_layers,
                                           base_gpu=base_gpu)
        bw_cell = self._build_encoder_cell(hparams,
                                           num_bi_layers,
                                           num_bi_residual_layers,
                                           base_gpu=(base_gpu + num_bi_layers))

        bi_outputs, bi_state = tf.nn.bidirectional_dynamic_rnn(
            fw_cell,
            bw_cell,
            inputs,
            dtype=dtype,
            sequence_length=sequence_length,
            time_major=self.time_major,
            swap_memory=True)

        return tf.concat(bi_outputs, -1), bi_state

    def _build_decoder_cell(self, hparams, encoder_outputs, encoder_state,
                            source_sequence_length):
        """Build an RNN cell that can be used by decoder."""
        # We only make use of encoder_outputs in attention-based models
        if hparams.attention:
            raise ValueError("BasicModel doesn't support attention.")

        cell = model_helper.create_rnn_cell(
            unit_type=hparams.unit_type,
            num_units=hparams.num_units,
            num_layers=self.num_decoder_layers,
            num_residual_layers=self.num_decoder_residual_layers,
            forget_bias=hparams.forget_bias,
            dropout=hparams.dropout,
            num_gpus=self.num_gpus,
            mode=self.mode,
            single_cell_fn=self.single_cell_fn)

        # For beam search, we need to replicate encoder infos beam_width times
        if self.mode == tf.contrib.learn.ModeKeys.INFER and hparams.beam_width > 0:
            decoder_initial_state = tf.contrib.seq2seq.tile_batch(
                encoder_state, multiplier=hparams.beam_width)
        else:
            decoder_initial_state = encoder_state

        return cell, decoder_initial_state
