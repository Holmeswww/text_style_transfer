# -*- coding: utf-8 -*-

# Copyright 2019 "Style Transfer for Texts: to Err is Human, but Error Margins Matter" Authors. All Rights Reserved.
#
# It's a modified code from
# Toward Controlled Generation of Text, ICML2017
# Zhiting Hu, Zichao Yang, Xiaodan Liang, Ruslan Salakhutdinov, Eric Xing
# https://github.com/asyml/texar/tree/master/examples/text_style_transfer
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
NN Model.
"""
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

# pylint: disable=invalid-name, too-many-locals

import tensorflow as tf

import texar.tf as tx
from texar.tf.modules import WordEmbedder, UnidirectionalRNNEncoder, \
        MLPTransformConnector, AttentionRNNDecoder, \
        GumbelSoftmaxEmbeddingHelper, Conv1DClassifier
from texar.tf.core import get_train_op
from texar.tf.utils import collect_trainable_variables, get_batch_size


def dynamic_padding(inp, format):

    pad_size = tf.shape(format)[1] - tf.shape(inp)[1]
    paddings = [[0, 0], [0, pad_size], [0, 0]] # assign here, during graph execution
    return tf.pad(inp, paddings, constant_values=0)

class CtrlGenModel(object):

    def __init__(self, inputs, vocab, gamma, lambda_g, lambda_z, lambda_z1, lambda_z2, lambda_ae, hparams=None):
        self._hparams = tx.HParams(hparams, None)
        self._build_model(inputs, vocab, gamma, lambda_g, lambda_z, lambda_z1, lambda_z2, lambda_ae)

    def _build_model(self, inputs, vocab, gamma, lambda_g, lambda_z, lambda_z1, lambda_z2, lambda_ae):

        embedder = WordEmbedder(
            vocab_size=vocab.size,
            hparams=self._hparams.embedder)

        encoder = UnidirectionalRNNEncoder(hparams=self._hparams.encoder)

        enc_text_ids = inputs['text_ids'][:, 1:]
        enc_outputs, final_state = encoder(embedder(enc_text_ids),
                                           sequence_length=inputs['length']-1)

        z = final_state[:, self._hparams.dim_c:]

        # -------------------- CLASSIFIER ---------------------

        n_classes = self._hparams.num_classes
        z_classifier_l1 = MLPTransformConnector(256, hparams=self._hparams.z_classifier_l1)
        z_classifier_l2 = MLPTransformConnector(64, hparams=self._hparams.z_classifier_l2)
        z_classifier_out = MLPTransformConnector(n_classes if n_classes > 2 else 1)

        z_logits = z_classifier_l1(z)
        z_logits = z_classifier_l2(z_logits)
        z_logits = z_classifier_out(z_logits)
        z_pred = tf.greater(z_logits, 0)
        z_logits = tf.reshape(z_logits, [-1])

        z_pred = tf.to_int64(tf.reshape(z_pred, [-1]))

        loss_z_clas = tf.nn.sigmoid_cross_entropy_with_logits(
            labels=tf.to_float(inputs['labels']), logits=z_logits)
        loss_z_clas = tf.reduce_mean(loss_z_clas)

        accu_z_clas = tx.evals.accuracy(labels=inputs['labels'], preds=z_pred)

        # -------------------________________---------------------


        label_connector = MLPTransformConnector(self._hparams.dim_c)

        labels = tf.to_float(tf.reshape(inputs['labels'], [-1, 1]))

        c = label_connector(labels)
        c_ = label_connector(1 - labels)

        h = tf.concat([c, z], 1)
        h_ = tf.concat([c_, z], 1)

        # Teacher-force decoding and the auto-encoding loss for G

        decoder = AttentionRNNDecoder(
            memory=enc_outputs,
            memory_sequence_length=inputs['length']-1,
            cell_input_fn=lambda inputs, attention: inputs,
            vocab_size=vocab.size,
            hparams=self._hparams.decoder)

        connector = MLPTransformConnector(decoder.state_size)

        g_outputs, _, _ = decoder(
            initial_state=connector(h), inputs=inputs['text_ids'],
            embedding=embedder, sequence_length=inputs['length']-1)

        loss_g_ae = tx.losses.sequence_sparse_softmax_cross_entropy(
            labels=inputs['text_ids'][:, 1:],
            logits=g_outputs.logits,
            sequence_length=inputs['length']-1,
            average_across_timesteps=True,
            sum_over_timesteps=False)
        
        ppl = tf.exp(loss_g_ae)

        # Gumbel-softmax decoding, used in training

        start_tokens = tf.ones_like(inputs['labels']) * vocab.bos_token_id

        end_token = vocab.eos_token_id

        gumbel_helper = GumbelSoftmaxEmbeddingHelper(
            embedder.embedding, start_tokens, end_token, gamma)

        soft_outputs_, _, soft_length_, = decoder(
            helper=gumbel_helper, initial_state=connector(h_))

        soft_outputs, _, soft_length, = decoder(
            helper=gumbel_helper, initial_state=connector(h))


        # ---------------------------- SHIFTED LOSS -------------------------------------
        _, encoder_final_state_ = encoder(embedder(soft_ids=soft_outputs_.sample_id),
                                          sequence_length=inputs['length'] - 1)
        _, encoder_final_state = encoder(embedder(soft_ids=soft_outputs.sample_id),
                                         sequence_length=inputs['length'] - 1)
        new_z_ = encoder_final_state_[:, self._hparams.dim_c:]
        new_z = encoder_final_state[:, self._hparams.dim_c:]

        cos_distance_z_ = tf.abs(
            tf.losses.cosine_distance(tf.nn.l2_normalize(z, axis=1), tf.nn.l2_normalize(new_z_, axis=1), axis=1))
        cos_distance_z = tf.abs(
            tf.losses.cosine_distance(tf.nn.l2_normalize(z, axis=1), tf.nn.l2_normalize(new_z, axis=1), axis=1))
        # ----------------------------______________-------------------------------------


        # Greedy decoding, used in eval

        outputs_, _, length_ = decoder(
            decoding_strategy='infer_greedy', initial_state=connector(h_),
            embedding=embedder, start_tokens=start_tokens, end_token=end_token)

        # Creates classifier

        classifier = Conv1DClassifier(hparams=self._hparams.classifier) #Conv1DClassifier(hparams=self._hparams.classifier)
        discriminator = Conv1DClassifier(hparams=self._hparams.discriminator)

        clas_embedder = WordEmbedder(vocab_size=vocab.size,
                                     hparams=self._hparams.embedder)

        # Classification loss for the classifier
        true_samples = clas_embedder(ids=inputs['text_ids'][:, 1:])
        clas_logits, clas_preds = discriminator(
            inputs=true_samples,
            sequence_length=inputs['length']-1)
        # print(clas_logits.shape)
        clas_logits, d_logits = tf.split(clas_logits,2,1)
        clas_logits = tf.squeeze(clas_logits)
        d_logits = tf.squeeze(d_logits)
        if self._hparams.WGAN:
            loss_d_clas = tf.nn.sigmoid_cross_entropy_with_logits(
                labels=tf.to_float(inputs['labels']), logits=clas_logits)
            accu_d_r = tx.evals.accuracy(labels=inputs['labels'], preds=(clas_logits>=0.5))

            loss_d_dis = -tf.reduce_mean(d_logits)
            # Classification loss for the generator, based on soft samples
            fake_samples = clas_embedder(soft_ids=soft_outputs_.sample_id)
            soft_logits, _ = discriminator(
                inputs=fake_samples,
                sequence_length=soft_length_)
            clas_logits, d_logits = tf.split(soft_logits,2,1)
            clas_logits = tf.squeeze(clas_logits)
            d_logits = tf.squeeze(d_logits)
            d_logits = d_logits - tf.reduce_max(d_logits, axis = 0, keepdims = True)
            if self._hparams.WWGAN:
                # W = tf.math.softmax(d_logits, axis = 0)
                W = tf.exp(d_logits) / tf.reduce_sum(tf.exp(d_logits), 0)
                W = tf.stop_gradient(W)
                loss_d_dis = loss_d_dis + tf.reduce_mean(d_logits*W)
            else:
                loss_d_dis = loss_d_dis + tf.reduce_mean(d_logits) # tf.reduce_mean(loss_g_clas)
            loss_d_clas = loss_d_clas + lambda_g * tf.nn.sigmoid_cross_entropy_with_logits(
                labels=tf.to_float(1-inputs['labels']), logits=clas_logits)
            accu_d_f = tx.evals.accuracy(labels=1-inputs['labels'], preds=(clas_logits>=0.5))

            loss_d_clas = tf.reduce_mean(loss_d_clas)
        else:
            loss_d_clas = tf.nn.sigmoid_cross_entropy_with_logits(
                labels=tf.to_float(inputs['labels']), logits=clas_logits)

            loss_d_clas = tf.reduce_mean(loss_d_clas)
            loss_d_dis = tf.constant(0., tf.float32)

        clas_logits, clas_preds = classifier(
            inputs=true_samples,
            sequence_length=inputs['length']-1)
        loss_clas = tf.nn.sigmoid_cross_entropy_with_logits(
                labels=tf.to_float(inputs['labels']), logits=clas_logits)

        loss_clas = tf.reduce_mean(loss_clas)
        accu_clas = tx.evals.accuracy(labels=inputs['labels'], preds=clas_preds)

        # Classification loss for the generator, based on soft samples
        fake_samples = clas_embedder(soft_ids=soft_outputs_.sample_id)
        soft_logits, _ = discriminator(
            inputs=fake_samples,
            sequence_length=soft_length_)
        
        soft_logits, d_logits = tf.split(soft_logits,2,1)
        soft_logits = tf.squeeze(soft_logits)
        d_logits = tf.squeeze(d_logits)
        if self._hparams.WGAN:
            loss_g_clas = tf.nn.sigmoid_cross_entropy_with_logits(
                labels=tf.to_float(1-inputs['labels']), logits=soft_logits)
            loss_g_clas = tf.reduce_mean(loss_g_clas) # tf.reduce_mean(loss_g_clas)
            loss_g_dis = -tf.reduce_mean(d_logits)
        else:
            loss_g_clas = tf.nn.sigmoid_cross_entropy_with_logits(
                labels=tf.to_float(1-inputs['labels']), logits=soft_logits)

            loss_g_clas = tf.reduce_mean(loss_g_clas)
            loss_g_dis = tf.constant(0., tf.float32)

        if self._hparams.WGAN:
            # WGAN-GP loss
            alpha = tf.random_uniform(shape=[get_batch_size(inputs['text_ids']), 1, 1], minval=0., maxval=1.)
            true_samples = tf.cond(tf.less(tf.shape(fake_samples)[1], tf.shape(true_samples)[1]), true_fn=lambda: true_samples, false_fn=lambda: dynamic_padding(true_samples, fake_samples))
            fake_samples = tf.cond(tf.less(tf.shape(fake_samples)[1], tf.shape(true_samples)[1]), true_fn=lambda: dynamic_padding(fake_samples, true_samples), false_fn=lambda: fake_samples)
            differences = fake_samples - true_samples  # fake_samples[:,:16] - true_samples[:,:16]
            interpolates = true_samples + (alpha * differences)
            # D(interpolates, is_reuse=True)
            soft_logits, _ = discriminator(
                inputs=interpolates,
                sequence_length=inputs['length']-1)
            _, d_logits = tf.split(soft_logits,2,1)
            d_logits = tf.squeeze(d_logits)
            gradients = tf.gradients(d_logits, [interpolates])[0]
            slopes = tf.sqrt(tf.reduce_sum(tf.square(gradients), reduction_indices=[1, 2]))
            gradient_penalty = self._hparams.LAMBDA * tf.reduce_mean((slopes-1.)**2)
        else:
            gradient_penalty = tf.constant(0., tf.float32)

        # Accuracy on soft samples, for training progress monitoring
        soft_logits, soft_preds = classifier(
            inputs=fake_samples,
            sequence_length=soft_length_)
        accu_g = tx.evals.accuracy(labels=1-inputs['labels'], preds=soft_preds)

        # Accuracy on greedy-decoded samples, for training progress monitoring

        _, gdy_preds = classifier(
            inputs=clas_embedder(ids=outputs_.sample_id),
            sequence_length=length_)

        accu_g_gdy = tx.evals.accuracy(
            labels=1-inputs['labels'], preds=gdy_preds)

        # Aggregates losses

        loss_g = lambda_ae * loss_g_ae + \
                 lambda_g * (self._hparams.ACGAN_SCALE_G*loss_g_clas + loss_g_dis) + \
                 lambda_z1 * cos_distance_z + cos_distance_z_ * lambda_z2 \
                 - lambda_z * loss_z_clas
        loss_d = self._hparams.ACGAN_SCALE_D*loss_d_clas + loss_d_dis + gradient_penalty
        print("\n==========LAMBDA: {}, ACSCALE  D:{}, G:{}=========\n".format(self._hparams.LAMBDA,self._hparams.ACGAN_SCALE_D,self._hparams.ACGAN_SCALE_G))
        loss_z = loss_z_clas

        # Creates optimizers

        g_vars = collect_trainable_variables(
            [embedder, encoder, label_connector, connector, decoder])
        d_vars = collect_trainable_variables([clas_embedder, discriminator])
        clas_vars = collect_trainable_variables([clas_embedder, classifier])
        z_vars = collect_trainable_variables([z_classifier_l1, z_classifier_l2, z_classifier_out])

        train_op_g = get_train_op(
            loss_g, g_vars, hparams=self._hparams.opt)
        train_op_g_ae = get_train_op(
            loss_g_ae, g_vars, hparams=self._hparams.opt)
        if self._hparams.WGAN:
            train_op_d = get_train_op(
                loss_d, d_vars, hparams=self._hparams.opt_d)
        else:
            train_op_d = get_train_op(
                loss_d, d_vars, hparams=self._hparams.opt)
        train_op_c = get_train_op(
            loss_clas, d_vars, hparams=self._hparams.opt)
        train_op_z = get_train_op(
            loss_z, z_vars, hparams=self._hparams.opt
        )

        # Interface tensors
        self.losses = {
            "loss_g": loss_g,
            "loss_g_ae": loss_g_ae,
            "loss_g_clas": loss_g_clas,
            "loss_g_dis": loss_g_dis,
            "loss_d": loss_d,
            "loss_d_clas": loss_d_clas,
            "loss_d_dis": loss_d_dis,
            "loss_clas": loss_clas,
            "loss_gp": gradient_penalty,
            "loss_z_clas": loss_z_clas,
            "loss_cos_": cos_distance_z_,
            "loss_cos": cos_distance_z
        }
        self.metrics = {
            "ppl": ppl,
            "accu_d_r": accu_d_r,
            "accu_d_f": accu_d_f,
            "accu_clas": accu_clas,
            "accu_g": accu_g,
            "accu_g_gdy": accu_g_gdy,
            "accu_z_clas": accu_z_clas
        }
        self.train_ops = {
            "train_op_g": train_op_g,
            "train_op_g_ae": train_op_g_ae,
            "train_op_d": train_op_d,
            "train_op_z": train_op_z,
            "train_op_c": train_op_c
        }
        self.samples = {
            "original": inputs['text_ids'][:, 1:],
            "transferred": outputs_.sample_id,
            "z_vector": z,
            "labels_source": inputs['labels'],
            "labels_target": 1 - inputs['labels'],
            "labels_predicted": gdy_preds
        }

        self.fetches_train_g = {
            "loss_g": self.train_ops["train_op_g"],
            "loss_g_ae": self.losses["loss_g_ae"],
            "loss_g_clas": self.losses["loss_g_clas"],
            "loss_g_dis": self.losses["loss_g_dis"],
            "loss_shifted_ae1": self.losses["loss_cos"],
            "loss_shifted_ae2": self.losses["loss_cos_"],
            "ppl": self.metrics["ppl"],
            "accu_g": self.metrics["accu_g"],
            "accu_g_gdy": self.metrics["accu_g_gdy"],
            "accu_z_clas": self.metrics["accu_z_clas"]
        }

        self.fetches_train_z = {
            "loss_z": self.train_ops["train_op_z"],
            "accu_z": self.metrics["accu_z_clas"]
        }

        self.fetches_train_d = {
            "loss_d": self.train_ops["train_op_d"],
            "loss_d_clas": self.losses["loss_d_clas"],
            "loss_d_dis": self.losses["loss_d_dis"],
            "loss_c": self.train_ops["train_op_c"],
            "loss_gp": self.losses["loss_gp"],
            "accu_d_r": self.metrics["accu_d_r"],
            "accu_d_f": self.metrics["accu_d_f"],
            "accu_clas": self.metrics["accu_clas"]
        }
        fetches_eval = {"batch_size": get_batch_size(inputs['text_ids'])}
        fetches_eval.update(self.losses)
        fetches_eval.update(self.metrics)
        fetches_eval.update(self.samples)
        self.fetches_eval = fetches_eval
