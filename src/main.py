import code
import glob
import os
import time
from datetime import datetime

import numpy as np
import tensorflow as tf

import images
import unet
from constants import NUM_CHANNELS, IMG_PATCH_SIZE, FOREGROUND_THRESHOLD
from summary import Summary

tf.app.flags.DEFINE_integer('batch_size', 25, "Batch size of training instances")
tf.app.flags.DEFINE_boolean('dilated_layers', False, "Add dilated CNN layers")
tf.app.flags.DEFINE_float('dropout', 0.8, "Probability to keep an input")
tf.app.flags.DEFINE_boolean('ensemble_prediction', False, "Ensemble Prediction")
tf.app.flags.DEFINE_string('eval_data_dir', None, "Directory containing eval images")
tf.app.flags.DEFINE_integer('eval_every', 500, "Number of steps between evaluations")
tf.app.flags.DEFINE_boolean('eval_train', False, "Evaluate training data")
tf.app.flags.DEFINE_integer('gpu', -1, "GPU to run the model on")
tf.app.flags.DEFINE_boolean('image_augmentation', False, "Augment training set of images with transformations")
tf.app.flags.DEFINE_boolean('interactive', False, "Spawn interactive Tensorflow session")
tf.app.flags.DEFINE_string('logdir', os.path.abspath("./logdir"), "Directory where to write logfiles")
tf.app.flags.DEFINE_float('lr', 0.01, "Initial learning rate")
tf.app.flags.DEFINE_string('model_path', None, "Restore exact model path")
tf.app.flags.DEFINE_float('momentum', 0.9, "Momentum")
tf.app.flags.DEFINE_integer('num_epoch', 5, "Number of pass on the dataset during training")
tf.app.flags.DEFINE_integer('num_eval_images', 4, "Number of images to predict for an evaluation")
tf.app.flags.DEFINE_integer('num_gpu', 1, "Number of available GPUs to run the model on")
tf.app.flags.DEFINE_integer('num_layers', 5, "Number of layers of the U-Net")
tf.app.flags.DEFINE_integer('patch_size', 128, "Size of the prediction image")
tf.app.flags.DEFINE_integer('pred_batch_size', 2, "Batch size of batchwise prediction")
tf.app.flags.DEFINE_string('restore_date', None, "Restore the model from specific date")
tf.app.flags.DEFINE_integer('restore_epoch', None, "Restore the model from specific epoch")
tf.app.flags.DEFINE_boolean('restore_model', False, "Restore the model from previous checkpoint")
tf.app.flags.DEFINE_integer('root_size', 64, "Number of filters of the first U-Net layer")
tf.app.flags.DEFINE_string('rotation_angles', None, "Rotation angles")
tf.app.flags.DEFINE_string('save_path', os.path.abspath("./runs"),
                           "Directory where to write checkpoints, overlays and submissions")
tf.app.flags.DEFINE_integer('seed', 2017, "Random seed for reproducibility")
tf.app.flags.DEFINE_integer('stride', 16, "Sliding delta for patches")
tf.app.flags.DEFINE_string('train_data_dir', os.path.abspath("./data/training"),
                           "Directory containing training images/ groundtruth/")
tf.app.flags.DEFINE_integer('train_score_every', 1000, "Compute training score after the given number of iterations")

FLAGS = tf.app.flags.FLAGS


class Options(object):
    """Options used by our model."""

    def __init__(self):
        self.batch_size = FLAGS.batch_size
        self.dilated_layers = FLAGS.dilated_layers
        self.dropout = FLAGS.dropout
        self.ensemble_prediction = FLAGS.ensemble_prediction
        self.eval_data_dir = FLAGS.eval_data_dir
        self.eval_every = FLAGS.eval_every
        self.eval_train = FLAGS.eval_train
        self.gpu = FLAGS.gpu
        self.image_augmentation = FLAGS.image_augmentation
        self.interactive = FLAGS.interactive
        self.logdir = FLAGS.logdir
        self.lr = FLAGS.lr
        self.model_path = FLAGS.model_path
        self.momentum = FLAGS.momentum
        self.num_epoch = FLAGS.num_epoch
        self.num_eval_images = FLAGS.num_eval_images
        self.num_gpu = FLAGS.num_gpu
        self.num_layers = FLAGS.num_layers
        self.patch_size = FLAGS.patch_size
        self.pred_batch_size = FLAGS.pred_batch_size
        self.restore_date = FLAGS.restore_date
        self.restore_epoch = FLAGS.restore_epoch
        self.restore_model = FLAGS.restore_model
        self.root_size = FLAGS.root_size
        self.rotation_angles = None if not FLAGS.rotation_angles else [int(i) for i in FLAGS.rotation_angles.split(",")]
        self.save_path = FLAGS.save_path
        self.seed = FLAGS.seed
        self.stride = FLAGS.stride
        self.train_data_dir = FLAGS.train_data_dir
        self.train_score_every = FLAGS.train_score_every


class ConvolutionalModel:
    def __init__(self, options, session):
        self._options = options
        self._session = session

        np.random.seed(options.seed)
        tf.set_random_seed(options.seed)
        print(options.num_layers, options.patch_size)
        self.input_size = unet.input_size_needed(options.patch_size, options.num_layers)

        self.experiment_name = datetime.now().strftime("%Y-%m-%dT%Hh%Mm%Ss")
        experiment_path = os.path.abspath(os.path.join(options.save_path, self.experiment_name))
        summary_path = os.path.join(options.logdir, self.experiment_name)

        self._summary = Summary(options, session, summary_path)
        self.build_graph()

    def cross_entropy_loss(self, labels, pred_logits):
        """BCE loss"""
        cross_entropy = tf.nn.sparse_softmax_cross_entropy_with_logits(
            logits=pred_logits,
            labels=labels)
        loss = tf.reduce_mean(cross_entropy)

        return loss

    def optimize(self, loss):
        """Build the part of the graph to optimize the loss function."""
        opts = self._options

        learning_rate = tf.train.exponential_decay(opts.lr, self._global_step,
                                                   1000, 0.95, staircase=True)

        # Use simple momentum for the optimization.
        optimizer = tf.train.MomentumOptimizer(learning_rate, opts.momentum)
        train = optimizer.minimize(loss, global_step=self._global_step)
        return train, learning_rate

    def build_graph(self):
        """Build the graph for the full model."""
        opts = self._options

        # Global step: scalar, i.e., shape [].
        global_step = tf.Variable(0, name="global_step")
        self._global_step = global_step

        # data placeholders
        patches_node = tf.placeholder(tf.float32,
                                      shape=(opts.batch_size, self.input_size, self.input_size, NUM_CHANNELS),
                                      name="patches")
        labels_node = tf.placeholder(tf.int64,
                                     shape=(opts.batch_size, opts.patch_size, opts.patch_size),
                                     name="groundtruth")

        patches_node, labels_node = self.stochastic_images_augmentation(patches_node, labels_node)

        dropout_keep = tf.placeholder_with_default(1.0, shape=(), name="dropout_keep")
        self._dropout_keep = dropout_keep

        predict_logits = unet.forward(patches_node, root_size=opts.root_size, num_layers=opts.num_layers,
                                      dilated_layers=opts.dilated_layers, dropout_keep=dropout_keep)
        predictions = tf.nn.softmax(predict_logits, dim=3)
        predictions = predictions[:, :, :, 1]
        loss = self.cross_entropy_loss(labels_node, predict_logits)

        self._train, self._learning_rate = self.optimize(loss)

        self._loss = loss
        self._predictions = predictions
        self._patches_node = patches_node
        self._labels_node = labels_node
        self._predict_logits = predict_logits

        self._summary.initialize_eval_summary()
        self._summary.initialize_train_summary()
        self._summary.initialize_overlap_summary()
        self._summary.initialize_missclassification_summary()

        summary_scalars = {"loss": loss, "learning_rate": self._learning_rate}
        self.summary_op = self._summary.get_summary_op(summary_scalars)

        # Properly initialize all variables.
        tf.global_variables_initializer().run()
        tf.local_variables_initializer().run()

        self.saver = tf.train.Saver(max_to_keep=100)

    def stochastic_images_augmentation(self, imgs, masks):
        """Add stochastic transformation to imgs and masks:
        flip_ud, flip_lr, transpose, rotation by any 90 degree
        """
        original_imgs, original_masks = imgs, masks
        batch_size = int(imgs.shape[0])
        self._image_augmentation = tf.placeholder_with_default(False, shape=(), name='image_augmentation_flag')

        def apply_transform(transform, pim):
            proba, img, mask = pim
            return tf.cond(proba > 0.5, lambda: transform(img), lambda: img), \
                   tf.cond(proba > 0.5, lambda: transform(mask), lambda: mask)

        def stochastic_transform(transform, imgs, masks, name):
            proba = tf.random_uniform(shape=(batch_size,), name="should_" + name)
            imgs, masks = tf.map_fn(lambda pim: apply_transform(tf.image.flip_up_down, pim),
                                    [proba, imgs, masks],
                                    dtype=(imgs.dtype, masks.dtype))
            return imgs, masks

        with tf.variable_scope("data_augm"):
            masks = tf.expand_dims(masks, -1)
            imgs, masks = stochastic_transform(tf.image.flip_up_down, imgs, masks, name="flip_up_down")
            imgs, masks = stochastic_transform(tf.image.flip_left_right, imgs, masks, name="flip_up_down")
            imgs, masks = stochastic_transform(tf.image.transpose_image, imgs, masks, name="transpose")

            number_rotation = tf.cast(tf.floor(tf.random_uniform(shape=(batch_size,), name="number_rotation") * 4),
                                      tf.int32)
            imgs, masks = tf.map_fn(lambda kim: (tf.image.rot90(kim[1], kim[0]), tf.image.rot90(kim[2], kim[0])),
                                    [number_rotation, imgs, masks],
                                    dtype=(imgs.dtype, masks.dtype))
            masks = tf.squeeze(masks, -1)

        imgs, masks = tf.cond(self._image_augmentation,
                              lambda: (imgs, masks),
                              lambda: (original_imgs, original_masks))

        return imgs, masks

    def train(self, patches, labels_patches, imgs, labels):
        """Train the model for one epoch

        params:
            imgs: [num_images, img_height, img_width, num_channel]
            labels: [num_images, num_patches_side, num_patches_side]
        """
        opts = self._options

        labels_patches = (labels_patches >= 0.5) * 1.
        labels = (labels >= 0.5) * 1.

        num_train_patches = patches.shape[0]

        indices = np.arange(0, num_train_patches)
        np.random.shuffle(indices)

        num_errors = 0
        total = 0

        for batch_i, offset in enumerate(range(0, num_train_patches - opts.batch_size, opts.batch_size)):
            batch_indices = indices[offset:offset + opts.batch_size]
            feed_dict = {
                self._patches_node: patches[batch_indices, :, :, :],
                self._labels_node: labels_patches[batch_indices],
                self._dropout_keep: opts.dropout,
                self._image_augmentation: opts.image_augmentation,
            }

            summary_str, _, l, predictions, predictions, step = self._session.run(
                [self.summary_op, self._train, self._loss, self._predict_logits, self._predictions,
                 self._global_step],
                feed_dict=feed_dict)

            print("Batch {} Step {}".format(batch_i, step), end="\r")
            self._summary.add(summary_str, global_step=step)

            num_errors += np.abs(labels_patches[batch_indices] - predictions).sum()
            total += opts.batch_size
            self._summary.add_to_pixel_missclassification_summary(num_errors, total, self._global_step)

            # from time to time do full prediction on some images
            if step > 0 and step % opts.eval_every == 0:
                print()

                images_to_predict = imgs[:opts.num_eval_images, :, :, :]
                masks = self.predict(images_to_predict)
                overlays = images.overlays(images_to_predict, masks)
                pred_masks = ((masks > 0.5) * 1).squeeze()
                true_masks = labels[:opts.num_eval_images, :, :].squeeze()

                self._summary.add_to_eval_summary(masks, overlays, labels, self._global_step)
                self._summary.add_to_overlap_summary(true_masks, pred_masks, self._global_step)

            if step > 0 and step % opts.train_score_every == 0:
                self._summary.add_to_training_summary(self.predict(imgs), labels, self._global_step)

        self._summary.flush()

    def predict(self, imgs):
        """Run inference on `imgs` and return predicted masks

        imgs: [num_images, image_height, image_width, num_channel]
        returns: masks [num_images, images_height, image_width] with road probabilities
        """
        opts = self._options

        num_images = imgs.shape[0]
        print("Running prediction on {} images... ".format(num_images), end="")

        if opts.ensemble_prediction:
            print("Start data augmentation for prediction...")
            imgs = images.image_augmentation_ensemble(imgs)
            print("Done")
            num_images = imgs.shape[0]

        offset = int((unet.input_size_needed(opts.patch_size, opts.num_layers) - opts.patch_size) / 2)
        imgs_exp = images.mirror_border(imgs, offset)
        patches = images.extract_patches(imgs_exp,
                                         patch_size=unet.input_size_needed(opts.patch_size, opts.num_layers),
                                         predict_patch_size=opts.patch_size,
                                         stride=opts.stride)
        num_patches = patches.shape[0]
        num_channel = imgs.shape[3]

        # patches padding to have full batches
        if num_patches % opts.batch_size != 0:
            num_extra_patches = opts.batch_size - (num_patches % opts.batch_size)
            extra_patches = np.zeros((num_extra_patches, opts.patch_size, opts.patch_size, num_channel))
            patches = np.concatenate([patches, extra_patches], axis=0)

        num_batches = int(patches.shape[0] / opts.batch_size)
        eval_predictions = np.ndarray(shape=(patches.shape[0], opts.patch_size, opts.patch_size))

        for batch in range(num_batches):
            offset = batch * opts.batch_size

            feed_dict = {
                self._patches_node: patches[offset:offset + opts.batch_size, :, :, :],
            }
            eval_predictions[offset:offset + opts.batch_size, :, :] = self._session.run(self._predictions, feed_dict)

        # remove padding
        eval_predictions = eval_predictions[0:num_patches]
        patches_per_image = int(num_patches / num_images)

        # construct masks
        new_shape = (num_images, patches_per_image, opts.patch_size, opts.patch_size, 1)
        masks = images.images_from_patches(eval_predictions.reshape(new_shape), stride=opts.stride)

        if opts.ensemble_prediction:
            print("Invert Data augmentation and average predictions...")
            masks = images.invert_image_augmentation_ensemble(masks)
            print("Averaging done...")

        print("Prediction Done")
        return masks

    def predict_batchwise(self, imgs, pred_batch_size):
        masks = []
        for i in range(int(np.ceil(imgs.shape[0] / pred_batch_size))):
            start = i * pred_batch_size
            end = start + pred_batch_size
            masks.append(self.predict(imgs[start:end]))

        if len(masks) > 1:
            masks = np.concatenate(masks, axis=0)
            return masks
        else:
            return masks[0]

    def save(self, epoch=0):
        opts = self._options
        model_data_dir = os.path.abspath(
            os.path.join(opts.save_path, self.experiment_name, 'model-epoch-{:03d}.chkpt'.format(epoch)))
        saved_path = self.saver.save(self._session, model_data_dir)
        # create checkpoint
        print("Model saved in file: {}".format(saved_path))

    def restore(self, date=None, epoch=None, file=None):
        """Restores model from saved checkpoint

        date: which model should be restored (most recent if None)
        epoch: at which epoch model should be restored (most recent if None)
        file: provide directly the checkpoint file te restore
        """
        opts = self._options

        if file is not None:
            model_data_dir = file
        else:
            # get experiment name to restore from
            if date is None:
                dates = [date for date in glob.glob(os.path.join(opts.save_path, "*")) if os.path.isdir(date)]
                model_data_dir = sorted(dates)[-1]
            else:
                model_data_dir = os.path.abspath(os.path.join(opts.save_path, date))

            # get epoch construct final path
            if epoch is None:
                model_data_dir = os.path.abspath(os.path.join(model_data_dir, 'model-epoch-*.chkpt.meta'))
                model_data_dir = sorted(glob.glob(model_data_dir))[-1][:-5]
            else:
                model_data_dir = os.path.abspath(
                    os.path.join(model_data_dir, 'model-epoch-{:03d}.chkpt'.format(epoch)))

        self.saver.restore(self._session, model_data_dir)
        print("Model restored from from file: {}".format(model_data_dir))


def main(_):
    opts = Options()
    if opts.gpu == -1:
        config = tf.ConfigProto()
    else:
        config = tf.ConfigProto(device_count={'GPU': opts.num_gpu}, allow_soft_placement=True)

    print(opts.patch_size)
    with tf.Graph().as_default(), tf.Session(config=config) as session:
        device = '/device:CPU:0' if opts.gpu == -1 else '/device:GPU:{}'.format(opts.gpu)
        print("Running on device {}".format(device))
        with tf.device(device):
            model = ConvolutionalModel(opts, session)

        if opts.restore_model:
            if opts.model_path is not None:
                model.restore(file=opts.model_path)
                print("Restore model: {}".format(opts.model_path))
            else:
                print("Restore date: {}".format(opts.restore_date))
                model.restore(date=opts.restore_date, epoch=opts.restore_epoch)

        if opts.num_epoch > 0:
            train_images, train_groundtruth = images.load_train_data(opts.train_data_dir)
            print(len(train_images))
            input_size = unet.input_size_needed(opts.patch_size, opts.num_layers)
            offset = int((input_size - opts.patch_size) / 2)
            extended_images = images.expand_and_rotate(train_images, opts.rotation_angles, offset)
            patches = images.extract_patches(extended_images,
                                             patch_size=input_size,
                                             predict_patch_size=opts.patch_size,
                                             stride=opts.stride)

            print("Train on {} patches of size {}x{}".format(patches.shape[0], patches.shape[1], patches.shape[2]))

            train_groundtruth_exp = images.expand_and_rotate(train_groundtruth, opts.rotation_angles, 0)
            labels_patches = images.extract_patches(train_groundtruth_exp,
                                                    patch_size=opts.patch_size,
                                                    stride=opts.stride)

            print(
                "Train on {} groundtruth patches of size {}x{}".format(labels_patches.shape[0], labels_patches.shape[1],
                                                                       labels_patches.shape[2]))

            model._summary.add_to_eval_patch_summary(train_groundtruth)
            for i in range(opts.num_epoch):
                print("==== Train epoch: {} ====".format(i))
                tf.local_variables_initializer().run()  # Reset scores

                # Drop last dimension if input image is not PNG-8
                if(len(labels_patches.shape) == 4):
                    labels_patches = labels_patches[:, :, :, 0]
                if(len(train_groundtruth.shape) == 4):
                    train_groundtruth = train_groundtruth[:, :, :, 0]

                model.train(patches, labels_patches, train_images, train_groundtruth)  # Process one epoch
                model.save(i)  # Save model to disk

        if opts.eval_train:
            print("Evaluate Test")
            eval_images, eval_groundtruth = images.load_train_data(opts.train_data_dir)
            pred_masks = model.predict_batchwise(eval_images, opts.pred_batch_size)
            pred_labels = ((pred_masks > 0.5) * 1).squeeze(-1)
            pred_overlays = images.overlays(eval_images, pred_masks, fade=0.5)
            overlapped = images.overlap_pred_true(pred_labels, eval_groundtruth)
            error = images.overlapp_error(pred_labels, eval_groundtruth)

            images.save_all(pred_labels, opts.eval_data_dir, "eval_binary_pred_{:03d}.png", greyscale=True)
            images.save_all(pred_masks, opts.eval_data_dir, "eval_probability_pred_{:03d}.png", greyscale=True)
            images.save_all(pred_overlays, opts.eval_data_dir, "eval_overlays_pred_{:03d}.png")
            images.save_all(overlapped, opts.eval_data_dir, "eval_confusion_{:03d}.png")
            images.save_all(error, opts.eval_data_dir, "eval_orror_{:03d}.png", greyscale=True)

        if opts.eval_data_dir and not opts.eval_train:
            print("Running inference on eval data {}".format(opts.eval_data_dir))
            eval_images = images.load(opts.eval_data_dir)
            start = time.time()
            masks = model.predict_batchwise(eval_images, opts.pred_batch_size)
            stop = time.time()
            print("Prediction time:{} mins".format((stop - start) / 60))
            masks = images.quantize_mask(masks, patch_size=IMG_PATCH_SIZE, threshold=FOREGROUND_THRESHOLD)
            overlays = images.overlays(eval_images, masks, fade=0.4)
            save_dir = os.path.abspath(os.path.join(opts.save_path, model.experiment_name))
            images.save_all(overlays, save_dir)
            images.save_submission_csv(masks, save_dir, IMG_PATCH_SIZE)

            # Save model used for prediction
            saved_path = model.saver.save(model._session, save_dir + "-model.chkpt")

            model_info = "Model used for submission: " + ""

        if opts.interactive:
            code.interact(local=locals())


if __name__ == '__main__':
    tf.app.run()
